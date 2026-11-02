const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
let callbacks=[],finished=false,lost=false;
const context={activeRepoId:'repo',_bulkJobPollTimer:null,clearTimeout:()=>{},renderBulkJobProgress:(kind,job)=>{lost=!!job.connection_lost;},
 setTimeout:fn=>{callbacks.push(fn);return callbacks.length;},api:async()=>{throw new Error('temporary transport failure');},
 toast:()=>{},_BULK_JOB_KINDS:{test_reassessment:{jobPath:()=>'/job'}},finishBulkJob:()=>{finished=true;}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function pollBulkJob('),source.indexOf('async function finishBulkJob(')),context);
(async()=>{
 context.pollBulkJob('test_reassessment',{id:'job',status:'running'});
 await callbacks[0]();
 assert.equal(callbacks.length,2);
 assert(lost);assert(!finished);
 context.api=async()=>({id:'job',status:'success'});
 await callbacks[1]();
 assert(finished);assert(!lost);
 console.log('DS27 polling recovers after transient failure: 5 assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
