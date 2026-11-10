'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {openApp}=require('../scripts/archive_runtime.cjs');
const root=path.resolve(__dirname,'..');

test('packaged offline data exactly preserves all five source files',()=>{
  const {build}=require('../scripts/build_offline_snapshot.cjs');
  assert.equal(fs.readFileSync(path.join(root,'offline-saved-data.js'),'utf8').replace(/\r\n/g,'\n'),build());
});

test('GitHub publisher refuses any network even with a token',async()=>{
  const before=global.fetch;let calls=0;
  global.fetch=async()=>{calls++;throw new Error('Unexpected HTTP');};
  try{await assert.rejects(()=>require('../scripts/publish_prediction_archive.cjs').publishArchive({}),/paused by owner/);}
  finally{global.fetch=before;}
  assert.equal(calls,0);
});

test('normal startup, navigation, reconnect, manual checks and reload never fetch data',{timeout:180000},async()=>{
  const saved=fs.readFileSync(path.join(root,'data/predictions/2026-27.json'));
  const expected=JSON.parse(saved).records.length;
  const app=await openApp(root,{runner:false});
  try{
    await app.page.waitForFunction(()=>predictionInitialLoadComplete&&!predictionArchiveState.loading,{},{timeout:60000});
    assert.equal(await app.page.evaluate(()=>lockedPredictions.length),expected);
    const before=await app.page.evaluate(()=>JSON.stringify(lockedPredictions));
    for(const tab of ['main','results','schedule','pred2','players','prev','accuracy','accuracy-new'])
      await app.page.evaluate(tab=>sw(tab),tab);
    await app.page.clock.install();
    const state=await app.page.evaluate(async()=>{
      const calls=[],original=window.fetch;
      window.fetch=async url=>{calls.push(String(url));throw new Error('Unexpected HTTP');};
      try{
        await loadPublishedDataSnapshot();await loadMatchContextData();await loadPredictionEvidence();
        await loadPredictionArchive(true);await checkLiveSchedule();
        await checkAwaitingResults();await submitPredictionArchive();
        openPredictionArchiveSync();startAutomaticScheduleCheck();startAutomaticResultsCheck();
        scheduleNextAutomaticCheck(1);scheduleUpdateCountdown();
        window.dispatchEvent(new Event('online'));document.dispatchEvent(new Event('visibilitychange'));
        return {calls,dialog:!!document.getElementById('prediction-archive-dialog'),
          timers:[scheduleAutoCheckTimer,scheduleAutoCountdownTimer,livesportAutoCheckTimer,livesportAutoCountdownTimer],
          archiveError:predictionArchiveState.error,scheduleBadge:document.getElementById('auto-schedule-badge').textContent,
          archiveBadge:document.getElementById('prediction-archive-status').textContent};
      }finally{window.fetch=original;}
    });
    assert.deepEqual(state.calls,[]);assert.equal(state.dialog,false);
    assert.deepEqual(state.timers,[null,null,null,null]);assert.equal(state.archiveError,'');
    assert.match(state.scheduleBadge,/odpojeno/);assert.match(state.archiveBadge,/synchronizace vypnutá/);
    await app.page.clock.runFor(11*60*1000);
    assert.equal(await app.page.evaluate(()=>JSON.stringify(lockedPredictions)),before);
    await app.page.clock.resume();
    await app.page.reload({waitUntil:'load',timeout:90000});
    await app.page.waitForFunction(()=>predictionInitialLoadComplete&&!predictionArchiveState.loading,{},{timeout:60000});
    assert.equal(await app.page.evaluate(()=>lockedPredictions.length),expected);
    assert.deepEqual(app.requests.filter(r=>!['document','script','other'].includes(r.type)),[]);
    assert.ok(app.requests.every(r=>r.url.startsWith(app.url+'/')));
    assert.deepEqual(app.errors,[]);
  }finally{await app.close();}
  assert.deepEqual(fs.readFileSync(path.join(root,'data/predictions/2026-27.json')),saved);
});
