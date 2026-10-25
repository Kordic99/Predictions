'use strict';
const fs=require('node:fs');
const {execFileSync}=require('node:child_process');
const A=require('../prediction-archive-core.js');
const before=process.env.ARCHIVE_BASE_SHA||'';
const current=A.validateArchive(JSON.parse(fs.readFileSync(A.FILE,'utf8')));
if(before&&before!=='0'.repeat(40)){
  if(!/^[a-f0-9]{40}$/.test(before))throw new Error('Invalid archive comparison revision');
  const exists=execFileSync('git',['ls-tree','--name-only',before,'--',A.FILE],{encoding:'utf8'}).trim();
  if(exists){
    const previous=JSON.parse(execFileSync('git',['show',before+':'+A.FILE],{encoding:'utf8',maxBuffer:32*1024*1024}));
    A.assertAppendOnly(previous,current);
    console.log('All '+previous.records.length+' earlier snapshots remain unchanged.');
  }else console.log('Initial archive publication; no prior archive at the base revision.');
}else console.log('Validated current archive; no push base supplied.');
