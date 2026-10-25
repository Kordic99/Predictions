/* Shared, dependency-free archive rules. Used by the website and Node workflow. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.PredictionArchive=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const SEASON='2026/27';
  const FILE='data/predictions/2026-27.json';
  const JOURNAL='chance_prediction_journal_v1';
  const aliases={
    'FC Viktoria Plzeň':'Viktoria Plzeň','FC Slovan Liberec':'Slovan Liberec',
    'FC Zlín':'Zlín','FC Baník Ostrava':'Baník Ostrava','FK Teplice':'Teplice',
    'Bohemians Praha 1905':'Bohemians 1905','FC Zbrojovka Brno':'Zbrojovka Brno',
    'AC Sparta Praha':'Sparta Prague','SK Slavia Praha':'Slavia Prague',
    '1.FC Slovácko':'Slovácko','FK Jablonec':'Jablonec','SK Sigma Olomouc':'Sigma Olomouc',
    'FC Hradec Králové':'Hradec Králové','FK Pardubice':'Pardubice',
    'SK Artis Brno':'Artis Brno','FK Mladá Boleslav':'Mladá Boleslav'
  };
  const team=value=>aliases[value]||value;
  function stable(value){
    if(value===null||typeof value!=='object')return JSON.stringify(value);
    if(Array.isArray(value))return '['+value.map(stable).join(',')+']';
    return '{'+Object.keys(value).filter(k=>value[k]!==undefined).sort().map(k=>JSON.stringify(k)+':'+stable(value[k])).join(',')+'}';
  }
  function signature(lock){
    const copy={...lock};
    // Only these non-prediction/UI fields may differ between copies.
    for(const key of ['id','_archive','_detailsOpen','_details2Open'])delete copy[key];
    return stable(copy);
  }
  function fixtureFor(lock,fixtures){
    const fixture=fixtures.find(f=>String(f.officialMatchId)===String(lock.officialMatchId||lock.snapshot?.inputs?.officialMatchId||''))
      ||fixtures.find(f=>Number(f.round)===Number(lock.round)&&team(f.home)===team(lock.home)&&team(f.away)===team(lock.away));
    if(!fixture||team(fixture.home)!==team(lock.home)||team(fixture.away)!==team(lock.away)||Number(fixture.round)!==Number(lock.round))
      throw new Error('Prediction does not match an official fixture: '+lock.home+' – '+lock.away);
    if((lock.season||SEASON)!==SEASON)throw new Error('Unsupported prediction season');
    return fixture;
  }
  function pragueMinute(ms){
    const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Prague',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(ms));
    const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));
    return p.year+'-'+p.month+'-'+p.day+'T'+p.hour+':'+p.minute;
  }
  function kickoffMs(fixture){
    if(!/^\d{4}-\d{2}-\d{2}$/.test(fixture?.date||'')||!/^\d{2}:\d{2}$/.test(fixture?.time||fixture?.kickoff||''))return null;
    const wanted=fixture.date+'T'+(fixture.time||fixture.kickoff);
    const utc=Date.parse(wanted+'Z');
    // Both Czech UTC offsets, verified through IANA; never use the viewer's TZ.
    const candidates=[utc-3600000,utc-7200000].filter(ms=>pragueMinute(ms)===wanted);
    return candidates.length?Math.min(...candidates):null;
  }
  function validateLock(lock){
    if(!lock||lock.predSystem!=='P2'||(lock.season||SEASON)!==SEASON)throw new Error('Only current-season P2 snapshots are supported');
    if(!Number.isFinite(Date.parse(lock.lockedAt||lock.savedAt)))throw new Error('Missing original prediction timestamp');
    for(const key of ['hP','dP','aP'])if(typeof lock[key]!=='number'||!Number.isFinite(lock[key])||lock[key]<0||lock[key]>100)throw new Error('Invalid probability '+key);
    if(Math.abs(lock.hP+lock.dP+lock.aP-100)>1.5)throw new Error('Probabilities must total 100%');
    if(!['1X','X','X2'].includes(lock.predOutcome))throw new Error('Invalid prediction outcome');
    if(!/^\d{1,2}:\d{1,2}$/.test(lock.topScore||''))throw new Error('Invalid predicted score');
    if(lock.snapshot&&!lock.snapshot.legacy){
      if(lock.snapshot.lockedAt!==(lock.lockedAt||lock.savedAt))throw new Error('Snapshot timestamp mismatch');
      for(const key of ['hP','dP','aP'])if(lock.snapshot.calculation?.[key]!==lock[key])throw new Error('Snapshot probability mismatch');
    }
    return lock;
  }
  function eligibility(lock,fixture,receivedAt=null){
    const kickoff=kickoffMs(fixture),created=Date.parse(lock.lockedAt||lock.savedAt);
    if(kickoff===null)return {eligible:false,reason:'unknown_kickoff',kickoffAt:null};
    const kickoffAt=new Date(kickoff).toISOString();
    if(!Number.isFinite(created))return {eligible:false,reason:'unknown_creation_time',kickoffAt};
    if(created>=kickoff)return {eligible:false,reason:'created_after_kickoff',kickoffAt};
    if(receivedAt&&Date.parse(receivedAt)>=kickoff)return {eligible:false,reason:'received_after_kickoff',kickoffAt};
    return {eligible:true,reason:receivedAt?'server_received_before_kickoff':'legacy_export_timestamp',kickoffAt};
  }
  function validateArchive(archive){
    if(archive?.schemaVersion!==1||archive.season!==SEASON||!Array.isArray(archive.records))throw new Error('Invalid prediction archive');
    const ids=new Set();
    for(const record of archive.records){
      validateLock(record.lock);
      if(!/^[a-f0-9]{64}$/.test(record.recordId||'')||ids.has(record.recordId))throw new Error('Duplicate or invalid archive record ID');
      ids.add(record.recordId);
      if(!/^\d+$/.test(record.fixtureId||'')||typeof record.evaluation?.eligible!=='boolean')throw new Error('Incomplete archive record');
      if(!Number.isFinite(Date.parse(record.archivedAt)))throw new Error('Invalid archive timestamp');
      const k=Date.parse(record.evaluation.kickoffAt),t=Date.parse(record.lock.lockedAt||record.lock.savedAt);
      if(record.evaluation.eligible&&(!Number.isFinite(k)||t>=k))throw new Error('Post-kickoff record marked eligible');
    }
    return archive;
  }
  function appendRecords(archive,incoming){
    validateArchive(archive);
    const byId=new Map(archive.records.map(r=>[r.recordId,r]));
    for(const record of incoming){
      const prior=byId.get(record.recordId);
      if(prior&&stable(prior)!==stable(record))throw new Error('Attempt to overwrite immutable archive record '+record.recordId);
      if(!prior)byId.set(record.recordId,record);
    }
    return validateArchive({...archive,records:[...byId.values()]});
  }
  function assertAppendOnly(before,after){
    validateArchive(before);validateArchive(after);
    const next=new Map(after.records.map(r=>[r.recordId,r]));
    for(const old of before.records)if(!next.has(old.recordId)||stable(next.get(old.recordId))!==stable(old))throw new Error('Previously archived prediction removed or changed: '+old.recordId);
  }
  function primaryRecords(archive){
    const selected=new Map();
    for(const record of archive.records){
      const key=record.lock.season+'|'+record.fixtureId+'|'+record.lock.predSystem;
      const prior=selected.get(key);
      if(!prior||(record.evaluation.eligible&&!prior.evaluation.eligible)||
        (record.evaluation.eligible===prior.evaluation.eligible&&Date.parse(record.lock.lockedAt||record.lock.savedAt)<Date.parse(prior.lock.lockedAt||prior.lock.savedAt)))selected.set(key,record);
    }
    return [...selected.values()];
  }
  function mergeLocks(...groups){
    // Keep all distinct snapshots in the journal, including conflicting versions.
    const rows=new Map();
    for(const lock of groups.flat()){
      if(lock?.predSystem!=='P2')continue;
      validateLock(lock);
      const key=signature(lock);
      if(!rows.has(key))rows.set(key,lock);
    }
    return [...rows.values()];
  }
  return {SEASON,FILE,JOURNAL,team,stable,signature,fixtureFor,pragueMinute,kickoffMs,validateLock,eligibility,validateArchive,appendRecords,assertAppendOnly,primaryRecords,mergeLocks};
});
