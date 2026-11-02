import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {resolve} from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
// Optional developer contract check, Node >=24. Requires a trusted official checkout.
if(process.argv.length!==4) throw Error('Usage: node scripts/check_upstream.mjs TWIKOO_CHECKOUT SYNTHETIC_JSONL');
const checkout=resolve(process.argv[2]);
const expected='b13ef7c018b0cb1bb4b9ac82d98728322ae5e79b';
if(execFileSync('git',['-C',checkout,'rev-parse','HEAD'],{encoding:'utf8'}).trim()!==expected) throw Error('Expected official Twikoo 2.0.17 commit');
const root=checkout+'/packages/server-common/src/services/';
async function official(file) {
 let text=stripTypeScriptTypes(readFileSync(root+file,'utf8'));
 text=text.replace(/^import[\s\S]*?from\s*["'][^"']+["'];\s*$/gm,'');
 text="import {createHash} from 'node:crypto'; const md5=s=>createHash('md5').update(s).digest('hex'); const sha256=s=>createHash('sha256').update(s).digest('hex');\n"+text;
 return import('data:text/javascript;base64,'+Buffer.from(text).toString('base64'));
}
const imp=await official('import.ts');
const dto=await official('comment-dto.ts');
const docs=readFileSync(process.argv[3],'utf8').trim().split('\n').map(JSON.parse);
const imported=await imp.commentImportTwikoo(imp.jsonParse(JSON.stringify(docs)),()=>{});
if(JSON.stringify(imported)!==JSON.stringify(docs)) throw Error('Twikoo importer altered documents');
const parsed=await dto.parseComment(imported,undefined,{SHOW_UA:'false',SHOW_REGION:'false'},{});
const rootComment=parsed.find(c=>c.replies.length===2);
if(!rootComment || rootComment.replies[1].pid!==rootComment.replies[0].id || rootComment.replies[1].rid!==rootComment.id) throw Error('Reply grouping mismatch');
console.log(JSON.stringify({officialVersion:'2.0.17',documents:docs.length,roots:parsed.length,replies:parsed.reduce((n,c)=>n+c.replies.length,0),importPreserved:true}));
