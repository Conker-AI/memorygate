import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';

const url = process.argv[2] || 'http://localhost:8021/memories';
const label = process.argv[3] || '';
const agent = process.argv[4] || 'conker';
const adminKey = process.argv[5] || 'test-admin-key-123';
const actionsArg = process.argv[6] || ''; // ';'-separated: 'search:<q>' | 'click:<selector>' | 'clickText:<text>' | 'clickAt:x,y' | 'wait:<ms>'
const width = Number(process.argv[7]) || 1440;
const height = Number(process.argv[8]) || 900;

const dir = './temporary screenshots';
if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
const existing = fs.readdirSync(dir).filter((f) => f.startsWith('screenshot-'));
const nums = existing.map((f) => parseInt(f.match(/screenshot-(\d+)/)?.[1] || '0', 10));
const next = nums.length ? Math.max(...nums) + 1 : 1;
const filename = label ? `screenshot-${next}-${label}.png` : `screenshot-${next}.png`;
const outPath = path.join(dir, filename);

const browser = await puppeteer.launch();
const page = await browser.newPage();
await page.setViewport({ width, height });

await page.goto((new URL(url)).origin, { waitUntil: 'networkidle0' });
const keyInput = await page.$('input[type="password"]');
if (keyInput) {
  await keyInput.type(adminKey);
  await page.click('button[type="submit"]');
  await new Promise((r) => setTimeout(r, 600));
}
await page.evaluate((a) => localStorage.setItem('memorygate_selected_agent', a), agent);
await page.goto(url, { waitUntil: 'networkidle0' });
await new Promise((r) => setTimeout(r, 500));

const actions = actionsArg.split(';').filter(Boolean);

for (const action of actions) {
  if (action.startsWith('clickAt:')) {
    const [x, y] = action.slice('clickAt:'.length).split(',').map(Number);
    await page.mouse.click(x, y);
    await new Promise((r) => setTimeout(r, 500));
  } else if (action.startsWith('search:')) {
    const query = action.slice('search:'.length);
    const input = await page.$('input[placeholder="Search memories..."]');
    if (!input) { console.error(`search: input not found for "${query}"`); continue; }
    await input.type(query, { delay: 30 });
    await new Promise((r) => setTimeout(r, 900));
  } else if (action.startsWith('click:')) {
    const selector = action.slice('click:'.length);
    await page.click(selector);
    await new Promise((r) => setTimeout(r, 500));
  } else if (action.startsWith('type:')) {
    const [selector, ...rest] = action.slice('type:'.length).split('|');
    const value = rest.join('|');
    const input = await page.$(selector);
    if (input) await input.type(value, { delay: 20 });
    await new Promise((r) => setTimeout(r, 400));
  } else if (action.startsWith('wait:')) {
    await new Promise((r) => setTimeout(r, Number(action.slice('wait:'.length))));
  } else if (action.startsWith('clickText:')) {
    const text = action.slice('clickText:'.length);
    const clicked = await page.evaluate((t) => {
      const all = Array.from(document.querySelectorAll('button, a, [role="button"], div, span, p, h1, h2, h3'));
      const matches = all.filter((el) => el.textContent.trim().includes(t));
      matches.sort((a, b) => a.textContent.length - b.textContent.length);
      const target = matches[0];
      if (!target) return false;
      const clickable = target.closest('button, a, [role="button"]') || target;
      clickable.click();
      return true;
    }, text);
    if (!clicked) console.error(`clickText: no element found containing "${text}"`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

await page.screenshot({ path: outPath, fullPage: true });
await browser.close();
console.log(`Saved ${outPath}`);
