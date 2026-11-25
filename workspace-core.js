((root,factory)=>{const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.PredictionsWorkspaceCore=api;})(globalThis,()=>{
'use strict';

// Shared pure transport logic, deliberately independent of login tokens and
// Supabase. Browser and automation can exchange the same verified envelope.
const MAX_PACKED=6*1024*1024,MAX_PLAIN=60*1024*1024;
const hex=(bytes)=>Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
const digest=async bytes=>hex(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)));

function validateSnapshot(s){
  if(s?.app!=='chance-liga-dashboard'||!Number.isInteger(s.schemaVersion)||s.schemaVersion<7||
     !['players','results_current','results_extra','predMatches','lockedPredictions'].every(k=>Array.isArray(s[k]))||
     s.players.length>5000||s.results_current.length>400||s.results_extra.length>20000||
     s.predMatches.length>400||s.lockedPredictions.length>1000)throw Error('Neplatná struktura pracovního prostoru Predictions.');
  return s;
}

async function pack(snapshot){
  const raw=new TextEncoder().encode(JSON.stringify(validateSnapshot(snapshot)));
  if(raw.length>MAX_PLAIN)throw Error('Pracovní prostor přesahuje velikostní limit.');
  const compressed=new Uint8Array(await new Response(new Blob([raw]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer());
  if(compressed.length>MAX_PACKED)throw Error('Komprimovaný pracovní prostor přesahuje velikostní limit.');
  const blocks=[];
  for(let i=0;i<compressed.length;i+=24576)blocks.push(btoa(String.fromCharCode(...compressed.subarray(i,i+24576))));
  return {payload:blocks.join(''),compressed_sha256:await digest(compressed)};
}

async function unpack(envelope){
  const {payload,compressed_sha256:expected}=envelope||{};
  if(typeof payload!=='string'||payload.length>MAX_PACKED/3*4||payload.length%4!==0||
     !/^[A-Za-z0-9+/]+={0,2}$/.test(payload)||!/^[a-f0-9]{64}$/.test(expected||''))throw Error('Neplatný balík pracovního prostoru.');
  const compressed=Uint8Array.from(atob(payload),x=>x.charCodeAt(0));
  if(await digest(compressed)!==expected)throw Error('Kontrolní součet pracovního prostoru nesouhlasí.');
  const reader=new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
  let length=0;const blocks=[];
  for(;;){
    const {done,value}=await reader.read();if(done)break;
    length+=value.length;if(length>MAX_PLAIN){await reader.cancel();throw Error('Překročena velikost pracovního prostoru.');}
    blocks.push(value);
  }
  return validateSnapshot(JSON.parse(await new Blob(blocks).text()));
}

// Serialize commits. A confirmed revision only advances on server success;
// an error does not discard the pending snapshot or resolve the caller's save.
class WorkspaceWriter{
  constructor({revision=null,commit}){this.revision=revision;this.commit=commit;this.pending=null;this.retry=null;this.inFlight=null;}
  stage(snapshot,manualLocks=[]){
    validateSnapshot(snapshot);
    const locks=new Map((this.pending?.manualLocks||[]).map(l=>[String(l.officialMatchId),l]));
    for(const lock of manualLocks){
      const id=String(lock?.officialMatchId||'');if(!/^\d+$/.test(id))throw Error('Predikce nemá oficiální ID zápasu.');
      const previous=locks.get(id);if(previous&&JSON.stringify(previous)!==JSON.stringify(lock))throw Error('Stejný zápas byl uzamčen dvakrát.');
      locks.set(id,lock);
    }
    this.pending={snapshot:structuredClone(snapshot),manualLocks:structuredClone([...locks.values()])};
  }
  flush(){
    if(this.inFlight)return this.inFlight;
    if(!this.pending&&!this.retry)return Promise.resolve(this.revision);
    this.inFlight=this.drain().finally(()=>this.inFlight=null);
    return this.inFlight;
  }
  async drain(){
    while(this.pending||this.retry){
      const next=this.retry||this.pending;
      if(this.pending===next)this.pending=null;
      this.retry=next;
      // Keep the operation ID through connection timeouts for safe replays.
      next.operation ||= {revision:crypto.randomUUID(),...(await pack(next.snapshot))};
      const receipt=await this.commit({expected_revision:this.revision,...next.operation,manual_locks:next.manualLocks});
      if(receipt?.revision!==next.operation.revision)throw Error('Databáze nepotvrdila revizi.');
      this.revision=receipt.revision;
      this.retry=null;
    }
    return this.revision;
  }
}

return {validateSnapshot,pack,unpack,WorkspaceWriter};
});
