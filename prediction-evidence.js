/* Shared, deterministic time filtering and conservative factual context (no API/AI). */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.PredictionEvidence=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const VERSION='2026.11-facts-v1',DAY=86400000;
  const stamp=v=>typeof v==='number'?v:Date.parse(v);
  const http=v=>/^https?:\/\//i.test(String(v||''));
  function known(row,cutoff,fallback){
    const times=['knownAt','observedAt','fetchedAt','updatedAt','publishedAt'].filter(k=>row[k]!=null).map(k=>stamp(row[k]));
    if(!times.length)times.push(stamp(fallback));
    return times.every(t=>Number.isFinite(t)&&t<=stamp(cutoff));
  }
  function league(row){
    // Legacy Results contains league records only. Explicit cup/friendly tags never qualify.
    return !row.competition||/^(chance liga|fortuna liga|fortuna:liga|gambrinus liga|synot liga|1\.?\s*liga|czech first league)$/i.test(row.competition);
  }
  function chronological(rows,{cutoff,observedAt,kickoff,season}={}){
    const limit=stamp(cutoff),seen=new Set();
    return rows.filter(r=>r&&/^\d+:\d+$/.test(r.score||'')&&league(r)&&(!season||String(r.season)<=season))
      .map(r=>({...r,_playedAt:kickoff(r)}))
      .filter(r=>Number.isFinite(r._playedAt)&&r._playedAt<limit&&known(r,limit,observedAt)&&
        (r.completedAt?Number.isFinite(stamp(r.completedAt))&&stamp(r.completedAt)>r._playedAt&&stamp(r.completedAt)<=limit:
          ['finished','completed','FT'].includes(r.status)||r._playedAt+DAY<limit)&&
        !['scheduled','live','postponed','cancelled','abandoned'].includes(r.status))
      .sort((a,b)=>a._playedAt-b._playedAt||String(a.officialMatchId||a.livesportMatchId||'').localeCompare(String(b.officialMatchId||b.livesportMatchId||'')))
      .filter(r=>{const key=[r.season,r.h,r.a,r.r].join('|');if(seen.has(key))return false;seen.add(key);return true;});
  }
  const lastFive=(rows,team)=>rows.filter(r=>r.h===team||r.a===team).slice(-5).reverse();
  function validateFeed(feed){
    if(feed?.schemaVersion!==1||feed.season!=='2026/27'||!Number.isFinite(stamp(feed.fetchedAt))||!feed.teams||typeof feed.teams!=='object')throw new Error('Neplatné podklady Accuracy New.');
    for(const team of Object.values(feed.teams)){
      if(!Array.isArray(team.matches)||!Array.isArray(team.news)||team.matches.length>150||team.news.length>12)throw new Error('Neplatný rozsah podkladů.');
      for(const m of team.matches)if(!m.id||!Number.isFinite(stamp(m.kickoffAt))||!http(m.sourceUrl)||!['completed','scheduled'].includes(m.status)||!Number.isFinite(stamp(m.fetchedAt)))throw new Error('Neplatný zdroj zápasu v podkladech.');
      for(const n of team.news)if(!http(n.sourceUrl)||!Number.isFinite(stamp(n.publishedAt))||!Number.isFinite(stamp(n.fetchedAt))||n.effect!==0)throw new Error('Neplatný zdroj zprávy.');
      if(team.social&&!team.social.every(http))throw new Error('Neplatný sociální odkaz.');
    }
    return feed;
  }
  function sideContext(feed,team,target,cutoff){
    const limit=stamp(cutoff),start=stamp(target),row=feed?.teams?.[team];
    const fresh=row&&known(feed,limit)&&limit-stamp(feed.fetchedAt)<=2*DAY&&known(row,limit,feed.fetchedAt);
    const matches=[],seen=new Set();
    if(fresh)for(const m of row.matches||[]){
      const t=stamp(m.kickoffAt);
      if(!['completed','scheduled'].includes(m.status)||seen.has(m.id)||!known(m,limit)||!Number.isFinite(t)||t>=start||t<start-14*DAY||/friendly|přátel|pratel/i.test(m.competition||''))continue;
      if(m.status==='completed'&&(t>=limit||!Number.isFinite(stamp(m.completedAt))||stamp(m.completedAt)<=t||stamp(m.completedAt)>limit))continue;
      if(m.status==='scheduled'&&t<limit)continue; // stale unconfirmed fixture, not a played match
      seen.add(m.id);matches.push({...m});
    }
    matches.sort((a,b)=>stamp(a.kickoffAt)-stamp(b.kickoffAt));
    const covered=!!fresh&&row.coverage?.results==='ok'&&row.coverage?.fixtures==='ok';
    const last=matches.at(-1),restHours=last?Math.max(0,(start-(last.status==='completed'?stamp(last.completedAt):stamp(last.kickoffAt)+2*3600000))/3600000):null;
    // Experimental conservative prior, not a claim of a measured injury/performance effect.
    // Full coverage is required; partial feeds must NOT imply a rested/healthy team.
    const count7=matches.filter(m=>stamp(m.kickoffAt)>=start-7*DAY).length;
    const penalty=covered?Math.min(.05,(restHours!==null&&restHours<72?.03:0)+(count7>=3?.02:0)):0;
    const news=fresh?(row.news||[]).filter(n=>known(n,limit)&&limit-stamp(n.publishedAt)<=7*DAY).slice(0,6):[];
    return {team,coverage:covered?'verified':'partial-or-stale',restHours:restHours===null?null:Math.round(restHours*10)/10,
      count7,projected:matches.some(m=>m.status==='scheduled'),multiplier:1-penalty,matches,news,
      notes:['Absences already affect the expected XI; no second absence penalty.',
        'News, European scorelines and unverified rotation have no standalone numerical effect.'],
      checks:fresh?row.coverage||{}:{status:'missing-or-stale'},social:row?.social||[]};
  }
  function context(feed,home,away,target,cutoff){
    const h=sideContext(feed,home,target,cutoff),a=sideContext(feed,away,target,cutoff);
    return {version:VERSION,cutoff,feedCheckedAt:feed&&known(feed,cutoff)?feed.fetchedAt:null,home:h,away:a,
      policy:'Verified workload only (0 to -5% own expected goals). News informational; no sentiment, win bonus or duplicated absence penalty.'};
  }
  function metrics(p,score){
    if(!/^\d+:\d+$/.test(score||''))return null;
    const [h,a]=score.split(':').map(Number),idx=h>a?0:h===a?1:2,probs=[p.hP,p.dP,p.aP].map(x=>Number(x)/100);
    const outcome=['1','X','2'][idx],tip=p.predOutcome||p.mainTip;
    return {brier:probs.reduce((s,x,i)=>s+(x-(idx===i?1:0))**2,0),logLoss:-Math.log(Math.max(1e-12,probs[idx])),
      correct:tip==='1X'?idx!==2:tip==='X2'?idx!==0:tip===outcome,
      hardCorrect:(p.hardTip||['1','X','2'][probs.indexOf(Math.max(...probs))])===outcome,
      exact:p.topScore===score};
  }
  return {VERSION,known,chronological,lastFive,validateFeed,context,metrics};
});
