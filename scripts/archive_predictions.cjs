'use strict';
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const A=require('../prediction-archive-core.js');
const {openApp}=require('./archive_runtime.cjs');
const ROOT=path.resolve(__dirname,'..');
const FILE=path.join(ROOT,A.FILE);
const digest=value=>crypto.createHash('sha256').update(value).digest('hex');
const read=file=>JSON.parse(fs.readFileSync(file,'utf8'));
const empty=()=>({schemaVersion:1,season:A.SEASON,policy:{automatic:'Disabled. Predictions are locked manually by the user; the server only archives submitted snapshots.',evaluation:'Original pre-kickoff exports or server-received pre-kickoff predictions only.',selection:'Earliest eligible snapshot per official fixture; alternate versions remain archived.'},records:[]});

function makeRecord(lock,fixture,source,now,legacy=false){
  A.validateLock(lock);
  if(Date.parse(lock.lockedAt||lock.savedAt)>Date.parse(now)+60000)throw new Error('Future prediction timestamp');
  const evaluation=A.eligibility(lock,fixture,legacy?null:now);
  return {recordId:digest(A.signature(lock)),fixtureId:String(fixture.officialMatchId),archivedAt:now,source,evaluation,lock};
}

function writeArchive(file,data){
  A.validateArchive(data);
  for(const record of data.records)if(record.recordId!==digest(A.signature(record.lock)))throw new Error('Archive content hash mismatch');
  const text=JSON.stringify(data,null,2)+'\n';
  if(fs.existsSync(file)&&fs.readFileSync(file,'utf8')===text)return false;
  fs.mkdirSync(path.dirname(file),{recursive:true});
  // Atomic replacement only after full validation; original version stays in Git.
  fs.writeFileSync(file+'.tmp',text);fs.renameSync(file+'.tmp',file);
  return true;
}

async function main(){
  const [mode='validate',...files]=process.argv.slice(2);
  let archive=fs.existsSync(FILE)?A.validateArchive(read(FILE)):empty();
  const schedule=read(path.join(ROOT,'schedule-live.json'));
  if(schedule.matches.length!==240||new Set(schedule.matches.map(m=>String(m.officialMatchId))).size!==240)throw new Error('Schedule must contain 240 unique fixtures');
  for(let r=1;r<=30;r++)if(schedule.matches.filter(m=>Number(m.round)===r).length!==8)throw new Error('Invalid schedule round '+r);
  if(mode==='validate'||mode==='generate'){
    // Old queued workflows may still request generate. Validate without creating
    // anything, so the transition is safe and never silently re-enables locking.
    for(const record of archive.records)if(record.recordId!==digest(A.signature(record.lock)))throw new Error('Archive content hash mismatch');
    if(mode==='generate')console.log('Automatic prediction generation is disabled; manual locking only.');
    console.log('Validated '+archive.records.length+' immutable records.');return;
  }
  if(!['recover','submit'].includes(mode))throw new Error('Unknown archive mode');
  const app=await openApp(ROOT);
  const additions=[];
  const before=archive.records.length;
  const ids=new Set(archive.records.map(r=>r.recordId));
  function add(lock,source,now,legacy=false){
    const fixture=A.fixtureFor(lock,schedule.matches);
    const record=makeRecord(lock,fixture,source,now,legacy);
    if(!ids.has(record.recordId)){additions.push(record);ids.add(record.recordId);}
  }
  async function verify(locks){
    locks.forEach(A.validateLock);
    await app.page.evaluate(locks=>{
      for(const lock of locks)if(!lockedPredictionIntegrityOK(lock))throw new Error('Invalid original snapshot integrity: '+lock.home+' – '+lock.away);
    },locks);
  }
  try{
    if(mode==='recover'){
      if(!files.length)throw new Error('Pass explicit user-provided JSON exports for recovery');
      for(const file of files){
        const raw=fs.readFileSync(file,'utf8'),data=JSON.parse(raw);
        const locks=(data.lockedPredictions||[]).filter(l=>l.predSystem==='P2'&&(l.season||A.SEASON)===A.SEASON);
        await verify(locks);
        for(const lock of locks)add(lock,{kind:'recovered-export',file:path.basename(file),sha256:digest(raw),exportedAt:data.savedAt||null},new Date().toISOString(),true);
      }
    }else if(mode==='submit'){
      const request=JSON.parse(process.env.PREDICTION_SUBMISSION||'{}');
      if(!Array.isArray(request.locks)||!request.locks.length||request.locks.length>10)throw new Error('Invalid submitted prediction list');
      if(Buffer.byteLength(JSON.stringify(request))>60000)throw new Error('Submission too large');
      await verify(request.locks);
      for(const raw of request.locks){
        const lock={...raw};delete lock._archive;
        add(lock,{kind:'authenticated-browser',actor:process.env.GITHUB_ACTOR||'local',runId:process.env.GITHUB_RUN_ID||null},new Date().toISOString());
      }
    }
    archive=A.appendRecords(archive,additions);
    writeArchive(FILE,archive);
    const late=archive.records.filter(r=>!r.evaluation.eligible).length;
    console.log(JSON.stringify({mode,before,added:additions.length,total:archive.records.length,excluded:late}));
    if(app.errors.length)throw new Error(app.errors.join('\n'));
  }finally{await app.close();}
}
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={makeRecord,writeArchive,empty,digest};
