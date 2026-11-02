// Optional source comparison: Node >=24, trusted Nekomi and github-slugger 2.0.0 checkouts.
// Outputs only synthetic expectations. No network or database calls.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { stripTypeScriptTypes } from 'node:module';
if (process.argv.length !== 4) throw Error('Usage: node scripts/check_identity.mjs NEKOMI_ROOT GITHUB_SLUGGER_ROOT');
const source = readFileSync(resolve(process.argv[2], 'src/integrations/comments/identity.ts'), 'utf8');
const { slug } = await import(pathToFileURL(resolve(process.argv[3], 'index.js')));
const { commentKey } = await import('data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64'));
const cases = [
  ['hello.md', {}], ['Sub Dir/Hello_WORLD!.md', {}], ['目录/你好😀.md', {}],
  ['folder/index.md', {}], ['renamed.md', {slug: 'about'}],
  ['renamed.md', {commentId: '原来的 ID'}], ['renamed.md', {commentPath: '/archives/12.html'}], ['index.md', {}],
];
const output = cases.map(([file, data]) => {
  // Astro 7.4.2 glob.js + utils.js default ID expression, inspected in official npm package.
  const id = data.slug || file.slice(0, -3).split('/').map(s => slug(s)).join('/').replace(/\/index$/, '');
  return { file, data, id, key: commentKey({id, data}, '/posts/') };
});
console.log(JSON.stringify(output));
