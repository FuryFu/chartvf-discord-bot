import hashlib
import json
import multiprocessing as mp
import re
import shutil
import sqlite3
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright

# Use Railway's persistent volume when it is mounted.
# Otherwise keep the existing local Windows/Linux behavior.
RAILWAY_VOLUME = Path('/data')
BASE_DIR = (RAILWAY_VOLUME / 'trader_reviews') if RAILWAY_VOLUME.is_dir() else Path('trader_reviews')
RAW_DIR = BASE_DIR / 'raw'
EXPORTS_DIR = BASE_DIR / 'exports'
DB_PATH = BASE_DIR / 'reviews.db'
for p in (BASE_DIR, RAW_DIR, EXPORTS_DIR): p.mkdir(parents=True, exist_ok=True)

class ReviewDownloadError(RuntimeError): pass
class NotionChallengeError(ReviewDownloadError): pass
class ReviewValidationError(ReviewDownloadError): pass


def initialize_database():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL, discord_username TEXT NOT NULL,
            discord_message_id TEXT NOT NULL, discord_message_url TEXT,
            notion_url TEXT UNIQUE NOT NULL, page_title TEXT, review_text TEXT,
            posted_at TEXT NOT NULL, downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archive_folder TEXT)''')
        cols = [r[1] for r in conn.execute('PRAGMA table_info(reviews)')]
        if 'archive_folder' not in cols:
            conn.execute('ALTER TABLE reviews ADD COLUMN archive_folder TEXT')


def review_exists(notion_url):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute('SELECT 1 FROM reviews WHERE notion_url=?', (notion_url,)).fetchone() is not None


def save_review(discord_user_id, discord_username, discord_message_id, discord_message_url,
                notion_url, page_title, review_text, posted_at, archive_folder):
    if not review_text or len(review_text.strip()) < 50:
        raise ReviewValidationError('Refusing to save a review with empty/too-short text.')
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('''INSERT OR IGNORE INTO reviews
            (discord_user_id,discord_username,discord_message_id,discord_message_url,notion_url,
             page_title,review_text,posted_at,archive_folder) VALUES (?,?,?,?,?,?,?,?,?)''',
            (str(discord_user_id), discord_username, str(discord_message_id), discord_message_url,
             notion_url, page_title, review_text, posted_at, str(archive_folder)))
        return cur.rowcount > 0


def reset_review_archive():
    """Delete imported DB rows and raw archives. Use only after explicit user confirmation."""
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('DELETE FROM reviews')
    if RAW_DIR.exists():
        shutil.rmtree(RAW_DIR)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*]', '_', value)
    value = re.sub(r'\s+', '_', value.strip())
    return value[:100] or 'unknown'


def clean_notion_text(text):
    junk = {'Skip to content', 'Get Notion free'}
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip() and line.strip() not in junk)


def get_extension_from_url(url):
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in {'.png','.jpg','.jpeg','.webp','.gif'} else '.jpg'


def review_identifier(notion_url):
    compact = notion_url.replace('-', '')
    m = re.search(r'([0-9a-fA-F]{32})(?:[/?#]|$)', compact)
    return m.group(1)[:10].lower() if m else hashlib.sha1(notion_url.encode()).hexdigest()[:10]


def normalize_notion_navigation_url(notion_url):
    parts = urlsplit(notion_url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _looks_like_challenge(title, text):
    blob = f'{title}\n{text}'.lower()
    markers = ['just a moment', 'checking your browser', 'verify you are human', 'security verification']
    return any(m in blob for m in markers)


class ReviewBrowserSession:
    """Reuse one native Playwright Chromium session for many Notion reviews."""

    def __init__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)

        # IMPORTANT: do not override user_agent here.
        # Diagnostic #2 proved that Notion accepts Playwright's native
        # Chromium 153 identity, while the old hard-coded Chrome 125 UA
        # caused older workspaces to redirect to unsupported-browser.html.
        self._context = self._browser.new_context(
            viewport={'width': 1440, 'height': 1000},
        )

    def close(self):
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._playwright.stop()

    def download(self, notion_url, trader_name='TestTrader', posted_at=None, max_attempts=4):
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                return self._download_once(notion_url, trader_name, posted_at)
            except (NotionChallengeError, ReviewValidationError, ReviewDownloadError) as exc:
                last_error = exc
                if attempt == max_attempts:
                    break

                wait = [10, 30, 60][min(attempt - 1, 2)]
                print(
                    f'Review attempt failed: {exc}. '
                    f'Retrying in {wait}s ({attempt}/{max_attempts})...'
                )
                time.sleep(wait)

        raise last_error or ReviewDownloadError('Review download failed')

    def _download_once(self, notion_url, trader_name, posted_at):
        posted_at = posted_at or datetime.now().isoformat()
        short_id = review_identifier(notion_url)

        review_folder = (
            RAW_DIR
            / safe_filename(trader_name)
            / f'{posted_at[:10]}_{short_id}_Review'
        )
        temp_folder = review_folder.with_name(
            review_folder.name + '_INCOMPLETE'
        )

        if temp_folder.exists():
            shutil.rmtree(temp_folder)
        temp_folder.mkdir(parents=True, exist_ok=True)

        navigation_url = normalize_notion_navigation_url(notion_url)

        print(f'Opening Notion page: {notion_url}')
        print(f'Navigation URL: {navigation_url}')

        page = self._context.new_page()

        # Capture image responses that Chromium itself receives while rendering
        # the Notion page. We save these response objects later, so we do not
        # issue a second HTTP request for every chart/screenshot.
        captured_image_responses = {}

        def capture_image_response(response):
            try:
                content_type = response.headers.get('content-type', '').lower()
                if (
                    response.status == 200
                    and 'image' in content_type
                    and response.url
                    and not response.url.startswith('data:')
                    and not response.url.lower().split('?', 1)[0].endswith('.svg')
                ):
                    captured_image_responses[response.url] = response
            except Exception:
                pass

        page.on('response', capture_image_response)

        try:
            # Diagnostic #2 proved "commit" is sufficient. Some older public
            # Notion pages never satisfied domcontentloaded in headless mode
            # even though their complete review content was already rendered.
            try:
                response = page.goto(
                    navigation_url,
                    wait_until='commit',
                    timeout=30000,
                )
            except Exception as exc:
                raise ReviewDownloadError(
                    f'Navigation did not commit: {type(exc).__name__}: {exc}'
                ) from exc

            status = response.status if response else None
            if status is not None and status >= 400:
                raise ReviewDownloadError(f'Notion returned HTTP {status}')

            print('Waiting for meaningful Notion content...')

            # Wait on actual rendered text rather than document lifecycle.
            deadline = time.time() + 20
            clean_text = ''
            title = ''

            while time.time() < deadline:
                current_url = page.url
                if 'unsupported-browser' in current_url.lower():
                    raise ReviewValidationError(
                        'Notion redirected to unsupported-browser.html'
                    )

                try:
                    title = page.title().strip()
                except Exception:
                    title = ''

                try:
                    raw_text = page.evaluate(
                        "() => document.body ? (document.body.innerText || '') : ''"
                    ) or ''
                except Exception:
                    raw_text = ''

                clean_text = clean_notion_text(raw_text)

                if _looks_like_challenge(title, clean_text):
                    raise NotionChallengeError(
                        f'Notion challenge page detected (title={title!r})'
                    )

                if len(clean_text) >= 50:
                    break

                page.wait_for_timeout(500)

            if len(clean_text) < 50:
                raise ReviewValidationError(
                    f'Extracted only {len(clean_text)} text characters'
                )

            # Scroll through the full page to materialize lazy-loaded Notion
            # image blocks. Stop once the document height is stable.
            previous_height = 0
            stable_rounds = 0

            for _ in range(40):
                current_height = page.evaluate(
                    "() => document.body ? document.body.scrollHeight : 0"
                )

                if current_height == previous_height:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                previous_height = current_height

                page.evaluate(
                    "() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)"
                )
                page.wait_for_timeout(500)

                if stable_rounds >= 2:
                    break

            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            # Re-extract after scrolling in case more Notion blocks rendered.
            title = page.title().strip()
            raw_text = page.evaluate(
                "() => document.body ? (document.body.innerText || '') : ''"
            ) or ''
            clean_text = clean_notion_text(raw_text)

            if 'unsupported-browser' in page.url.lower():
                raise ReviewValidationError(
                    'Notion redirected to unsupported-browser.html'
                )
            if _looks_like_challenge(title, clean_text):
                raise NotionChallengeError(
                    f'Notion challenge page detected (title={title!r})'
                )
            if len(clean_text) < 50:
                raise ReviewValidationError(
                    f'Extracted only {len(clean_text)} text characters'
                )

            html = page.content()

            (temp_folder / 'review.txt').write_text(
                clean_text,
                encoding='utf-8',
            )
            (temp_folder / 'page.html').write_text(
                html,
                encoding='utf-8',
            )

            # Save the actual rendered review images directly from Chromium.
            # This avoids a second request to Notion's image CDN and avoids
            # trying to match Notion's rewritten/proxied network URLs.
            img_locator = page.locator('img')
            image_count = img_locator.count()
            print(f'Found {image_count} rendered image candidates.')

            downloaded = []
            failed_images = []

            for idx in range(image_count):
                img = img_locator.nth(idx)

                try:
                    src = img.evaluate(
                        """img => img.currentSrc || img.src || ''"""
                    )
                    if not src or src.startswith('data:'):
                        continue
                    if src.lower().split('?', 1)[0].endswith('.svg'):
                        continue

                    # Make sure lazy-loaded images have actually rendered.
                    img.scroll_into_view_if_needed(timeout=10000)
                    page.wait_for_timeout(500)

                    dimensions = img.evaluate(
                        """img => ({
                            naturalWidth: img.naturalWidth || 0,
                            naturalHeight: img.naturalHeight || 0,
                            width: img.getBoundingClientRect().width || 0,
                            height: img.getBoundingClientRect().height || 0
                        })"""
                    )

                    # Ignore tiny Notion UI/icon/avatar images. Trader charts and
                    # screenshots are materially larger than these.
                    if (
                        dimensions['naturalWidth'] < 200
                        or dimensions['naturalHeight'] < 120
                    ):
                        print(
                            f'Skipping small UI image {idx + 1}: '
                            f"{dimensions['naturalWidth']}x"
                            f"{dimensions['naturalHeight']}"
                        )
                        continue

                    image_name = f'image_{len(downloaded) + 1:03d}.png'
                    image_path = temp_folder / image_name

                    # Element screenshots use the pixels Chromium has already
                    # rendered; they do not perform a second image GET.
                    img.screenshot(
                        path=str(image_path),
                        type='png',
                        timeout=20000,
                    )

                    if not image_path.exists() or image_path.stat().st_size < 1000:
                        raise ReviewValidationError(
                            'rendered image screenshot was empty or too small'
                        )

                    downloaded.append(image_name)
                    print(
                        f'Saved rendered image: {image_name} '
                        f"({dimensions['naturalWidth']}x"
                        f"{dimensions['naturalHeight']})"
                    )

                except Exception as exc:
                    failed_images.append(idx + 1)
                    print(
                        f'Could not save rendered image {idx + 1}: {exc}'
                    )

            if failed_images:
                raise ReviewValidationError(
                    f'{len(failed_images)} rendered review images failed '
                    f'to save: {failed_images}'
                )

            if image_count > 0 and not downloaded:
                raise ReviewValidationError(
                    'Page contained images but no review-sized rendered images '
                    'were saved'
                )

            image_urls = downloaded

            metadata = {
                'trader': trader_name,
                'posted_at': posted_at,
                'notion_url': notion_url,
                'navigation_url': navigation_url,
                'page_title': title,
                'review_id': short_id,
                'text_characters': len(clean_text),
                'images': downloaded,
                'image_candidates': len(downloaded),
                'image_capture_method': 'rendered_element_screenshot',
                'validated': True,
            }

            (temp_folder / 'metadata.json').write_text(
                json.dumps(metadata, indent=2),
                encoding='utf-8',
            )

            if review_folder.exists():
                shutil.rmtree(review_folder)

            temp_folder.rename(review_folder)

            print('========== DOWNLOAD COMPLETE ==========')
            print(f'Title: {title}')
            print(f'Text characters: {len(clean_text)}')
            print(
                f'Images saved: {len(downloaded)}/{len(image_urls)}'
            )
            print(f'Archive: {review_folder.resolve()}')
            print('=======================================')

            return {
                'title': title,
                'text': clean_text,
                'archive_folder': str(review_folder),
                'images': downloaded,
                'text_characters': len(clean_text),
            }

        except Exception:
            if temp_folder.exists():
                shutil.rmtree(temp_folder, ignore_errors=True)
            raise
        finally:
            page.close()



def _download_worker(conn, notion_url, trader_name, posted_at):
    """Child-process entry point for a killable review download."""
    try:
        session = ReviewBrowserSession()
        try:
            result = session.download(
                notion_url, trader_name, posted_at, max_attempts=1
            )
        finally:
            session.close()
        conn.send(("ok", result))
    except BaseException as exc:
        try:
            conn.send(("error", type(exc).__name__, str(exc), traceback.format_exc()))
        except Exception:
            pass
    finally:
        conn.close()



def classify_review_error(exc):
    """Classify a failed review so the importer can choose a safe cooldown."""
    message = str(exc).lower()
    if '429' in message or 'too many requests' in message or 'rate limit' in message:
        return 'rate_limit'
    if 'challenge page' in message or 'just a moment' in message:
        return 'challenge'
    if 'hard timeout' in message or 'timed out' in message or 'timeout' in message:
        return 'timeout'
    return 'other'


def download_notion_review_hard_timeout(
    notion_url, trader_name='TestTrader', posted_at=None, timeout_seconds=300
):
    """Download in a separate process so a wedged browser can be killed."""
    ctx = mp.get_context('spawn')
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_download_worker,
        args=(child_conn, notion_url, trader_name, posted_at),
        daemon=False,
    )
    process.start()
    child_conn.close()

    try:
        if not parent_conn.poll(timeout_seconds):
            print(
                f'HARD TIMEOUT: review exceeded {timeout_seconds}s; '
                'terminating browser process and continuing.'
            )
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            raise ReviewDownloadError(
                f'Review exceeded hard timeout of {timeout_seconds} seconds'
            )

        try:
            payload = parent_conn.recv()
        except EOFError as exc:
            raise ReviewDownloadError(
                f'Review worker exited unexpectedly (exit code {process.exitcode})'
            ) from exc

        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

        if payload[0] == 'ok':
            return payload[1]

        _, error_type, error_message, error_traceback = payload
        raise ReviewDownloadError(
            f'{error_type}: {error_message}\nChild traceback:\n{error_traceback}'
        )
    finally:
        parent_conn.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def download_notion_review(notion_url, trader_name='TestTrader', posted_at=None):
    session=ReviewBrowserSession()
    try: return session.download(notion_url,trader_name,posted_at)
    finally: session.close()

if __name__ == '__main__':
    initialize_database(); print('Trader review database initialized.')
