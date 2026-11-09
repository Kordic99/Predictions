'use strict';
// The real browser model calculates snapshots; this module only selects and archives.
const fs=require('node:fs');
const path=require('node:path');
const A=require('../prediction-archive-core.js');
const {makeRecord,writeArchive,digest}=require('./archive_predictions.cjs');
const {openApp}=require('./archive_runtime.cjs');
const ROOT=path.resolve(__dirname,'..');
const INPUTS=['schedule-live.json','rosters-live.json','match-context-live.json','prediction-evidence-live.json','prediction-evidence.js','accuracy-new.js','index.html','prediction-archive-core.js'];
const read=file=>JSON.parse(fs.readFileSync(file,'utf8'));

function predictionWindow(now){
  const local=A.pragueMinute(now),date=local.slice(0,10);
  const day=new Date(date+'T12:00:00Z').getUTCDay();
  // Calendar-based Friday -> Thursday, not the next Friday on a weekday retry.
  // This also handles DST, month/year changes and catch-up runs through Thursday.
  const offset=-((day+2)%7);
  const shift=n=>new Date(Date.parse(date+'T12:00:00Z')+n*86400000).toISOString().slice(0,10);
  return {friday:shift(offset),thursday:shift(offset+6),
    active:day!==5||local.slice(11)>='08:00'};
}

function selectFixtures(schedule,archive,now){
  A.validateArchive(archive);
  const matches=schedule?.matches;
  if(schedule?.season!==A.SEASON||!Array.isArray(matches)||matches.length!==240||
     new Set(matches.map(f=>String(f.officialMatchId))).size!==240)throw new Error('Expected 240 unique official fixtures');
  for(let round=1;round<=30;round++)if(matches.filter(f=>Number(f.round)===round).length!==8)throw new Error('Invalid round '+round);
  const window=predictionWindow(now),existing=new Set(archive.records.map(r=>r.fixtureId));
  const selected=[],skipped=[];
  for(const fixture of matches){
    if(!fixture.date||fixture.date<window.friday||fixture.date>window.thursday)continue;
    const id=String(fixture.officialMatchId),kickoff=A.kickoffMs(fixture);
    const reason=existing.has(id)?'already_archived':fixture.score?'already_played':
      ['postponed','cancelled','date_tba'].includes(fixture.status)?'postponed':
      kickoff===null?'unknown_kickoff':kickoff<=now?'kickoff_passed':null;
    if(reason)skipped.push({fixtureId:id,reason});else selected.push(fixture);
  }
  selected.sort((a,b)=>A.kickoffMs(a)-A.kickoffMs(b)||String(a.officialMatchId).localeCompare(String(b.officialMatchId)));
  return {window,selected,skipped};
}

function verifyReceipt(receipt,root,now){
  if(receipt?.schemaVersion!==1||now-Date.parse(receipt.checkedAt)>2*3600000||
     Date.parse(receipt.checkedAt)>now+60000||!Number.isFinite(Date.parse(receipt.checkedAt)))throw new Error('Missing or expired fresh-input receipt');
  for(const check of ['official-schedule','official-rosters-and-values','livesport-stats','strict-context','public-evidence'])
    if(!receipt.checks?.includes(check))throw new Error('Missing input check: '+check);
  if(process.env.GITHUB_RUN_ID&&receipt.runId!==process.env.GITHUB_RUN_ID)throw new Error('Receipt belongs to another workflow run');
  for(const file of INPUTS)if(receipt.hashes?.[file]!==digest(fs.readFileSync(path.join(root,file))))throw new Error('Input changed since verification: '+file);
}

async function calculate(app,{schedule,rosters,context,evidence,archive,fixtures}){
  return app.page.evaluate(({schedule,rosters,context,evidence,archive,fixtures})=>{
    // No user's local storage is involved: this is a fresh isolated server browser.
    for(const f of schedule.matches){
      const target=OFFICIAL_SCHEDULE_2026_27.find(t=>String(t.officialMatchId)===String(f.officialMatchId));
      if(!target||PredictionArchive.team(f.home)!==target.home||PredictionArchive.team(f.away)!==target.away||Number(f.round)!==Number(target.round))throw new Error('Official fixture identity changed');
    }
    applyLiveScheduleData(schedule,false);
    applyPublishedRosterData(rosters,false);
    matchContextLive=validateMatchContextData(context);
    predictionEvidenceLive=PredictionEvidence.validateFeed(evidence);
    mergePublishedPredictionArchive(archive);
    clearPred3Cache();
    const locks=[];
    for(const f of fixtures){
      const ctx=predictionContextFixture(f);
      if(!ctx||ctx.date!==f.date||ctx.time!==f.time||PredictionArchive.team(ctx.home)!==PredictionArchive.team(f.home)||PredictionArchive.team(ctx.away)!==PredictionArchive.team(f.away))throw new Error('Missing/current context mismatch: '+f.officialMatchId);
      if(PredictionArchive.kickoffMs(f)<=Date.now()||findResultForPred({...f,home:PredictionArchive.team(f.home),away:PredictionArchive.team(f.away),season:CURRENT_SEASON}))throw new Error('Kickoff passed during calculation');
      const match={...f,home:PredictionArchive.team(f.home),away:PredictionArchive.team(f.away),season:CURRENT_SEASON,kickoff:f.time,hDisadv:0,aDisadv:0,hXI:[],aXI:[]};
      if(!predMatches.some(m=>healthPredictionKey(m,false)===healthPredictionKey(match,false)))predMatches.push(match);
      if(!lockPredictionSystem(match,'P2','friday_automatic',false))throw new Error('Could not lock '+f.officialMatchId);
      const lock=findLockedPrediction(match,'P2');
      if(!lockedPredictionIntegrityOK(lock))throw new Error('Invalid generated snapshot integrity');
      locks.push(JSON.parse(JSON.stringify(lock)));
    }
    return locks;
  },{schedule,rosters,context,evidence,archive,fixtures});
}

async function run({root=ROOT,receiptPath,dryRun=true,reportPath}={}){
  const archive=read(path.join(root,A.FILE)),schedule=read(path.join(root,'schedule-live.json'));
  const selection=selectFixtures(schedule,archive,Date.now());
  const report={dryRun,checkedAt:new Date().toISOString(),window:selection.window,
    selected:selection.selected.map(f=>String(f.officialMatchId)),skipped:selection.skipped,added:0,predictions:[]};
  const saveReport=()=>{if(reportPath)fs.writeFileSync(reportPath,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));return report;};
  if(!dryRun&&!selection.window.active){report.status='before_friday_check';return saveReport();}
  const receipt=read(receiptPath);verifyReceipt(receipt,root,Date.now());
  if(!selection.selected.length){report.status='no_new_period_fixtures';return saveReport();}
  const app=await openApp(root);
  try{
    const locks=await calculate(app,{schedule,archive,rosters:read(path.join(root,'rosters-live.json')),context:read(path.join(root,'match-context-live.json')),evidence:read(path.join(root,'prediction-evidence-live.json')),fixtures:selection.selected});
    if(app.errors.length)throw new Error(app.errors.join('\n'));
    verifyReceipt(receipt,root,Date.now());
    const now=new Date().toISOString();
    const currentWindow=predictionWindow(Date.now());
    if(!dryRun&&(!currentWindow.active||currentWindow.friday!==selection.window.friday))throw new Error('Publication window expired during calculation');
    const additions=locks.map(lock=>makeRecord(lock,A.fixtureFor(lock,schedule.matches),{
      kind:'friday-automatic',runId:process.env.GITHUB_RUN_ID||null,codeRef:process.env.GITHUB_SHA||null,
      period:{from:selection.window.friday,to:selection.window.thursday},inputChecks:receipt
    },now));
    if(additions.some(r=>!r.evaluation.eligible))throw new Error('Kickoff passed before archiving; no predictions written');
    report.predictions=additions.map(r=>({fixtureId:r.fixtureId,home:r.lock.home,away:r.lock.away,hP:r.lock.hP,dP:r.lock.dP,aP:r.lock.aP,score:r.lock.topScore,tip:r.lock.predOutcome,
      accuracyNew:r.lock.snapshot.calculation.contextVariant}));
    if(!dryRun){
      const next=A.appendRecords(archive,additions);A.assertAppendOnly(archive,next);
      writeArchive(path.join(root,A.FILE),next);report.added=additions.length;
    }
    report.status=dryRun?'preview_only':'ready_for_publication';return saveReport();
  }finally{await app.close();}
}
if(require.main===module){
  const args=process.argv.slice(2),value=flag=>args[args.indexOf(flag)+1];
  if(!args.includes('--receipt')||!args.includes('--report')||args.some((a,i)=>i%2===0&&!['--receipt','--report','--mode'].includes(a))){console.error('Usage: --receipt <file> --report <file> --mode preview|publish');process.exitCode=1;}
  else if(!['preview','publish'].includes(value('--mode'))){console.error('Explicit preview or publish mode required');process.exitCode=1;}
  else run({receiptPath:value('--receipt'),reportPath:value('--report'),dryRun:value('--mode')!=='publish'}).catch(e=>{console.error(e);process.exitCode=1;});
}
module.exports={predictionWindow,selectFixtures,verifyReceipt,calculate,run,INPUTS};
