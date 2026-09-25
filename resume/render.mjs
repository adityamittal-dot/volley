// node resume/render.mjs <resume.json> <out.pdf>
// Renders a resume JSON (config/resume.json schema) to a one-page, ATS-safe PDF.
// Exits non-zero if it overflows one page.
import { readFile } from 'node:fs/promises';
import { renderResumePdf, closeBrowser } from './render-lib.mjs';

const [src, out] = process.argv.slice(2);
try {
  const { pageCount } = await renderResumePdf(JSON.parse(await readFile(src, 'utf8')), out);
  console.log(`${out}: ${pageCount} page(s)`);
  process.exitCode = pageCount > 1 ? 2 : 0;
} finally {
  await closeBrowser();
}
