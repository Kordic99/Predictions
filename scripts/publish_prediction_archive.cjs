'use strict';
// CAS publication: concurrent roster/schedule or manual archive updates survive.
// This writes only the archive blob. No checkout resets, force pushes or secrets in files.
const fs=require('node:fs');
const A=require('../prediction-archive-core.js');
const {digest}=require('./archive_predictions.cjs');
const repo='Kordic99/Predictions';
const base='https://api.github.com/repos/'+repo;
async function request(route,method='GET',body){
  const response=await fetch(base+route,{method,headers:{Authorization:'Bearer '+process.env.GITHUB_TOKEN,Accept:'application/vnd.github+json','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
  if(!response.ok){const error=new Error('GitHub '+method+' '+route+': HTTP '+response.status);error.status=response.status;throw error;}
  return response.status===204?null:response.json();
}
async function publishArchive(local){
  if(!process.env.GITHUB_TOKEN)throw new Error('Missing authenticated workflow token');
  A.validateArchive(local);
  for(let attempt=0;attempt<5;attempt++){
    const ref=await request('/git/ref/heads/main');
    const commit=await request('/git/commits/'+ref.object.sha);
    const tree=await request('/git/trees/'+commit.tree.sha+'?recursive=1');
    if(tree.truncated)throw new Error('Cannot safely inspect truncated repository tree');
    const entry=tree.tree.find(x=>x.path===A.FILE);
    if(!entry)throw new Error('Archive seed missing from main; refusing to replace history');
    const blob=await request('/git/blobs/'+entry.sha);
    const remote=A.validateArchive(JSON.parse(Buffer.from(blob.content.replace(/\s/g,''),'base64').toString('utf8')));
    const byId=new Map(remote.records.map(r=>[r.recordId,r]));
    for(const row of [...local.records,...remote.records])if(digest(A.signature(row.lock))!==row.recordId)throw new Error('Corrupt archive content hash');
    const added=local.records.filter(r=>!byId.has(r.recordId));
    if(!added.length){console.log('All snapshots already archived; no commit.');return;}
    const merged=A.appendRecords(remote,added);
    try{
      const result=await request('/contents/'+A.FILE,'PUT',{message:'Archive '+added.length+' immutable prediction snapshot(s)',branch:'main',sha:entry.sha,content:Buffer.from(JSON.stringify(merged,null,2)+'\n').toString('base64')});
      console.log('Published '+result.commit.sha+'; added '+added.length+' snapshots.');return;
    }catch(error){if(![409,422].includes(error.status)||attempt===4)throw error;}
  }
}
if(require.main===module)publishArchive(JSON.parse(fs.readFileSync(A.FILE,'utf8'))).catch(error=>{console.error(error);process.exitCode=1;});
module.exports={publishArchive};
