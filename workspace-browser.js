((root,factory)=>{if(typeof module==='object'&&module.exports)module.exports=factory(require('./workspace-core.js'));
else root.PredictionsWorkspaceBrowser=factory(root.PredictionsWorkspaceCore);})(globalThis,Core=>{
'use strict';

// Shell adapter. The access token stays in the existing memory-only request
// closure; the iframe gets bounded state operations, never the token itself.
function createWorkspaceClient({request,readRuntime,applyRuntime,onStatus=()=>{}}){
  let writer=null,active=false;
  async function read(){
    const rows=await request('/rest/v1/predictions_workspace_state?select=revision,payload,compressed_sha256,updated_at&limit=1');
    if(!Array.isArray(rows)||rows.length!==1)throw Error('Společná databáze zatím není připravená. Lokální data nebyla změněna.');
    const locks=await request('/rest/v1/predictions_workspace_locks?select=season,fixture_id,origin,snapshot,committed_at&limit=500');
    if(!Array.isArray(locks))throw Error('Centrální archiv nebyl načten.');
    return {revision:rows[0].revision,updatedAt:rows[0].updated_at,locks,snapshot:await Core.unpack(rows[0])};
  }
  async function initialize(){
    if(active)throw Error('Společná databáze už je připojená.');
    const current=await read();
    await applyRuntime(current.snapshot,current.locks);
    writer=new Core.WorkspaceWriter({revision:current.revision,commit:body=>request('/functions/v1/predictions-workspace',{method:'POST',body})});
    active=true;onStatus({type:'saved',revision:current.revision,updatedAt:current.updatedAt});
  }
  const unsaved=()=>Boolean(writer?.pending||writer?.retry||writer?.inFlight);
  async function refresh(){
    if(!active||unsaved())return false;
    const current=await read();
    if(unsaved())return false;
    // Friday can add locks without changing the user revision. Reconcile both.
    await applyRuntime(current.snapshot,current.locks);
    writer.revision=current.revision;
    return true;
  }
  async function retry(){
    if(!active)throw Error('Společná databáze není připojená.');
    try{
      onStatus({type:'saving'});
      const revision=await writer.flush();
      onStatus({type:'saved',revision});return revision;
    }catch(error){
      onStatus({type:'error',error});throw error;
    }
  }
  async function save(manualLock){
    if(!active)throw Error('Společná databáze není připojená.');
    writer.stage(readRuntime(),manualLock?[manualLock]:[]);
    return retry();
  }
  return {initialize,refresh,save,retry,unsaved};
}
return {createWorkspaceClient};
});
