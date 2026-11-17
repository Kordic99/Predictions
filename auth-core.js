(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.PredictionsAuthCore=api;})(globalThis,function(){
  'use strict';
  function validateManifest(m){
    if(!m||m.id!=='current'||! /^[a-f0-9]{64}$/.test(m.release_id)||!Number.isInteger(m.part_count)||m.part_count<1||m.part_count>256||!Number.isInteger(m.compressed_bytes)||m.compressed_bytes<1||m.compressed_bytes>12582912||!Number.isInteger(m.html_bytes)||m.html_bytes<1||m.html_bytes>41943040)throw new Error('Neplatný manifest soukromé aplikace.');
    return m;
  }
  async function decodeBundle(manifest,parts){
    const m=validateManifest(manifest);
    if(!Array.isArray(parts)||parts.length!==m.part_count)throw new Error('Soukromá aplikace není kompletní.');
    const chunks=parts.map((p,i)=>{
      if(p.release_id!==m.release_id||p.part!==i||typeof p.payload!=='string'||p.payload.length>65536||! /^[A-Za-z0-9+/]+={0,2}$/.test(p.payload))throw new Error('Neplatná část soukromé aplikace.');
      return Uint8Array.from(atob(p.payload),c=>c.charCodeAt(0));
    });
    if(chunks.reduce((n,a)=>n+a.length,0)!==m.compressed_bytes)throw new Error('Nesouhlasí velikost soukromé aplikace.');
    const gz=new Uint8Array(m.compressed_bytes);let offset=0;for(const c of chunks){gz.set(c,offset);offset+=c.length;}
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',gz)),x=>x.toString(16).padStart(2,'0')).join('');
    if(hash!==m.release_id)throw new Error('Kontrolní součet soukromé aplikace nesouhlasí.');
    const reader=new Blob([gz]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
    let size=0;const raw=[];
    for(;;){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>m.html_bytes){await reader.cancel();throw new Error('Překročena velikost aplikace.');}raw.push(value);}
    if(size!==m.html_bytes)throw new Error('Nekompletní obsah aplikace.');
    return await new Blob(raw,{type:'text/html;charset=utf-8'}).text();
  }
  return {validateManifest,decodeBundle};
});
