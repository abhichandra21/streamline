// Capture the docs screenshots from a demo build (see demo/build.py).
//
//   node scripts/screenshots.mjs /tmp/streamline-demo
//
// Starts the web app in the demo copy with a dummy LLM key, waits for On Deck
// to finish refreshing, captures each page into src/assets/screenshots/ as JPEG
// (small enough to commit), then
// stops the app. PYTHON overrides the interpreter (default: the repo venv).
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../..');
const demo = process.argv[2];
if (!demo) throw new Error('usage: node scripts/screenshots.mjs <demo-dir>');
if (!process.env.TMDB_API_KEY) throw new Error('TMDB_API_KEY is not set');

const port = 5071; // Chrome refuses some ports near 5060, so not 5061.
const base = `http://127.0.0.1:${port}`;
const outDir = resolve(here, '../src/assets/screenshots');

const pages = [
	['home', '/'],
	['mood-match', '/wizard'],
	['find', '/find'],
	['on-deck', '/shows'],
	['archive', '/history'],
	['rate-it', '/loved-it'],
	['seen-it', '/classics'],
	['watchlist', '/watchlist'],
	['searches', '/searches', async (page) => {
		// Open the first search so its results show.
		await page.locator('details').first().evaluate((el) => { el.open = true; });
	}],
	['settings', '/settings'],
];

const env = { ...process.env, STREAMLINE_PORT: String(port), STREAMLINE_HOST: '127.0.0.1',
	ANTHROPIC_API_KEY: 'screenshots-make-no-llm-calls' };
for (const key of Object.keys(env)) {
	if (/^(PLEX_|GEMINI_|OPENAI_)/.test(key) || key === 'STREAMLINE_PASSWORD' || key === 'STREAMLINE_API_TOKEN') delete env[key];
}
const python = process.env.PYTHON || resolve(repo, 'venv/bin/python');
const app = spawn(python, ['-m', 'recommender.web'], { cwd: demo, env, stdio: 'ignore' });

async function waitFor(check, what, timeoutMs) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await check().catch(() => false)) return;
		await new Promise((r) => setTimeout(r, 2000));
	}
	throw new Error(`Timed out waiting for ${what}`);
}

try {
	await waitFor(async () => (await fetch(`${base}/healthz`)).ok, 'the web app to start', 60_000);
	// The first visit starts the On Deck refresh; capture only once it is done.
	await waitFor(async () => !/checking \d+ of/i.test(await (await fetch(`${base}/shows`)).text()),
		'On Deck to finish refreshing', 600_000);

	mkdirSync(outDir, { recursive: true });
	const browser = await chromium.launch();
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 });
	for (const [name, path, prepare] of pages) {
		await page.goto(base + path, { waitUntil: 'networkidle', timeout: 90_000 });
		if (prepare) await prepare(page);
		await page.waitForTimeout(1000);
		await page.screenshot({ path: `${outDir}/${name}.jpg`, type: 'jpeg', quality: 82 });
		console.log(`Captured ${name}`);
	}
	await browser.close();
} finally {
	app.kill();
}
