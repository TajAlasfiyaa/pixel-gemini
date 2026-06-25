"""
Google One automation using Selenium.

Logs into a Gmail account, navigates to Google One, detects the
12-month free Gemini Pro offer, and returns the activation / payment link.
"""

import logging
import os
import shutil
import time
import re
from urllib.parse import urlparse
from typing import Optional

import pyotp

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.core.os_manager import ChromeType
    HAS_WDM = True
except ImportError:
    HAS_WDM = False

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Chrome binary detection ───────────────────────────────────────────────────

def _find_chrome_binary() -> Optional[str]:
    """Auto-detect Chrome / Chromium binary path on the system."""
    candidates = [
        # Replit / nix
        os.environ.get("CHROME_BIN"),
        os.environ.get("CHROMIUM_BIN"),
        os.environ.get("GOOGLE_CHROME_BIN"),
        # Common Linux paths
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/nix/store/chromium/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            logger.info("Found Chrome binary at: %s", path)
            return path
    return None


# ── Driver factory ────────────────────────────────────────────────────────────

def _build_driver(profile: DeviceProfile) -> webdriver.Chrome:
    """Return a headless Chrome WebDriver configured for the device profile."""
    options = Options()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=390,844")  # Pixel 10 Pro screen size
    options.add_argument(f"--user-agent={profile.user_agent}")

    # Extra stability flags for headless Linux environments (Replit, etc.)
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--single-process")

    # Auto-detect Chrome / Chromium binary
    chrome_bin = _find_chrome_binary()
    if chrome_bin:
        options.binary_location = chrome_bin

    # Mobile emulation – Pixel 10 Pro viewport
    mobile_emulation = {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    }
    options.add_experimental_option("mobileEmulation", mobile_emulation)

    # Suppress automation flags
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    # Build the service – prefer webdriver-manager for version matching
    service = _create_service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


def _create_service() -> Service:
    """Create a ChromeDriver Service, using webdriver-manager when available."""
    if HAS_WDM:
        try:
            # Try Chromium first (common on Replit / nix)
            path = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
            logger.info("webdriver-manager installed chromedriver (Chromium) at: %s", path)
            return Service(path)
        except Exception:
            pass
        try:
            # Fall back to standard Chrome
            path = ChromeDriverManager().install()
            logger.info("webdriver-manager installed chromedriver (Chrome) at: %s", path)
            return Service(path)
        except Exception as exc:
            logger.warning("webdriver-manager failed (%s), falling back to PATH", exc)

    # Final fallback – let Selenium find chromedriver on PATH
    return Service()


# ── Login helper ──────────────────────────────────────────────────────────────

def _wait_for(driver: webdriver.Chrome, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> object:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _handle_2fa_challenge(driver: webdriver.Chrome, totp_secret: str) -> None:
    """Detect and handle Google's 2FA TOTP challenge after password entry.

    Uses multiple selector strategies since Google's DOM is dynamic:
    1. Check if we landed on a challenge page (URL or page content).
    2. If a challenge-method picker is shown, select the TOTP/Authenticator option.
    3. Find the OTP input field via name='totpPin', type='tel', or aria-label.
    4. Generate a fresh TOTP code with pyotp and submit.
    """
    try:
        current_url = driver.current_url.lower()

        # Detect 2FA challenge page by URL or page content
        is_challenge = (
            "challenge" in current_url
            or "signin/v2" in current_url
            or "2sv" in current_url
        )

        if not is_challenge:
            # Also check page source for TOTP-related content
            try:
                page_source_lower = driver.page_source.lower()
                is_challenge = any(kw in page_source_lower for kw in (
                    "2-step verification",
                    "authenticator",
                    "verification code",
                    "totppin",
                    "enter the code",
                ))
            except WebDriverException:
                pass

        if not is_challenge:
            logger.debug("No 2FA challenge detected – skipping.")
            return

        logger.info("2FA challenge page detected – handling TOTP entry.")

        # ── Step 1: If Google shows a picker for 2FA methods, select TOTP ─────
        _select_totp_method(driver)

        # ── Step 2: Find the OTP input field ──────────────────────────────────
        otp_input = _find_otp_input(driver)
        if otp_input is None:
            logger.warning("Could not locate the OTP input field.")
            return

        # ── Step 3: Generate and enter the TOTP code ──────────────────────────
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        logger.info("Generated TOTP code (first 2 digits: %s**)", code[:2])
        otp_input.clear()
        otp_input.send_keys(code)

        # ── Step 4: Click the "Next" / submit button ──────────────────────────
        _click_2fa_next(driver)
        time.sleep(3)

        logger.info("2FA code submitted successfully.")

    except Exception as exc:
        logger.warning("2FA challenge handling failed: %s", exc)


def _select_totp_method(driver: webdriver.Chrome) -> None:
    """If Google presents a list of 2FA methods, click the TOTP option."""
    totp_keywords = [
        "authenticator app",
        "authenticator",
        "google authenticator",
        "use your authenticator",
        "get a verification code",
    ]
    try:
        # Look for clickable elements that reference the authenticator
        for keyword in totp_keywords:
            try:
                el = driver.find_element(
                    By.XPATH,
                    f"//*[contains(translate(text(),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'), '{keyword}')]"
                )
                el.click()
                time.sleep(2)
                logger.info("Selected TOTP method via text: '%s'", keyword)
                return
            except NoSuchElementException:
                continue

        # Try data-challengetype attribute (Google sometimes uses this)
        try:
            el = driver.find_element(
                By.CSS_SELECTOR, '[data-challengetype="6"]'
            )
            el.click()
            time.sleep(2)
            logger.info("Selected TOTP method via data-challengetype.")
            return
        except NoSuchElementException:
            pass

    except Exception as exc:
        logger.debug("No 2FA method picker found (or already on TOTP page): %s", exc)


def _find_otp_input(driver: webdriver.Chrome):
    """Locate the OTP input field using multiple selector strategies."""
    selectors = [
        (By.CSS_SELECTOR, 'input[name="totpPin"]'),
        (By.CSS_SELECTOR, 'input#totpPin'),
        (By.CSS_SELECTOR, 'input[type="tel"]'),
        (By.XPATH, "//input[contains(@aria-label, 'code')]"),
        (By.XPATH, "//input[contains(@aria-label, 'Code')]"),
        (By.XPATH, "//input[contains(@aria-label, 'Enter')]"),
        (By.CSS_SELECTOR, 'input[name="pin"]'),
        (By.CSS_SELECTOR, 'input[name="idvPin"]'),
    ]
    for by, value in selectors:
        try:
            el = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((by, value))
            )
            logger.info("Found OTP input via %s='%s'", by, value)
            return el
        except (TimeoutException, NoSuchElementException):
            continue

    return None


def _click_2fa_next(driver: webdriver.Chrome) -> None:
    """Click the submit / next button on the 2FA page."""
    button_selectors = [
        (By.ID, "totpNext"),
        (By.ID, "idvPreregisteredPhoneNext"),
        (By.ID, "next"),
        (By.CSS_SELECTOR, '#totpNext button'),
        (By.CSS_SELECTOR, 'button[type="submit"]'),
        (By.XPATH, "//button[contains(., 'Next')]"),
        (By.XPATH, "//span[contains(., 'Next')]/ancestor::button"),
    ]
    for by, value in button_selectors:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((by, value))
            )
            btn.click()
            logger.info("Clicked 2FA next button via %s='%s'", by, value)
            return
        except (TimeoutException, NoSuchElementException):
            continue

    logger.warning("Could not find 2FA next/submit button – form may auto-submit.")


def _gmail_login(driver: webdriver.Chrome, email: str, password: str,
                 totp_secret: str) -> bool:
    """
    Perform Gmail / Google account login with TOTP 2FA.

    *totp_secret* is a base32-encoded TOTP secret key.  After the password
    step the function checks for a 2FA challenge and enters a freshly
    generated 6-digit code.

    Returns True on apparent success, False on detectable failure.
    """
    try:
        driver.get(config.GMAIL_LOGIN_URL)
        time.sleep(2)

        # ── Email step ────────────────────────────────────────────────────────
        email_field = _wait_for(driver, By.CSS_SELECTOR,
                                'input[type="email"]')
        email_field.clear()
        email_field.send_keys(email)

        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()
        time.sleep(2)

        # ── Password step ─────────────────────────────────────────────────────
        password_field = _wait_for(driver, By.CSS_SELECTOR,
                                   'input[type="password"]')
        password_field.clear()
        password_field.send_keys(password)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(3)

        # ── 2FA / TOTP step ───────────────────────────────────────────────────
        # Google may present a 2FA challenge after the password.
        # We look for the TOTP input field and enter a freshly generated code.
        _handle_2fa_challenge(driver, totp_secret)

        # ── Verify login ──────────────────────────────────────────────────────
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        if (
            hostname == "myaccount.google.com"
            or hostname.endswith(".google.com")
            and "/u/" in path
        ):
            logger.info("Login succeeded for %s", email)
            return True

        # Check for error messages
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]'
            )
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        # If we're no longer on the login page, assume success
        if not (
            hostname == "accounts.google.com"
            and path.startswith("/signin")
        ):
            logger.info("Login appeared successful for %s (URL: %s)",
                        email, current_url)
            return True

        logger.warning("Unexpected URL after login: %s", current_url)
        return False

    except TimeoutException as exc:
        logger.error("Timeout during login: %s", exc)
        return False
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the current page for a Gemini Pro offer / activation link.

    Strategy:
    1. Look for anchor tags whose text or aria-label contains offer keywords.
    2. Fall back to scanning all links for 'gemini' or 'upgrade' patterns.
    3. Return the first matching href found.
    """
    keywords = config.GEMINI_OFFER_KEYWORDS

    # -- Strategy 1: anchor text / aria-label match ---------------------------
    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            text = (link.text + " " + link.get_attribute("aria-label")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text match: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 2: URL pattern scan -----------------------------------------
    url_patterns = re.compile(
        r"(gemini|upgrade|activate|offer|redeem|trial|checkout)",
        re.IGNORECASE,
    )
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL pattern: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 3: button / CTA elements ------------------------------------
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
    for btn in buttons:
        try:
            text = btn.text.lower()
            if any(kw in text for kw in keywords):
                # Try to find parent anchor
                try:
                    parent_link = btn.find_element(By.XPATH, "ancestor::a")
                    href = parent_link.get_attribute("href") or ""
                    if href:
                        logger.info("Found offer link via button parent: %s", href)
                        return href
                except NoSuchElementException:
                    pass
                # Return current URL as fallback (user will land on offer page)
                logger.info("Found offer CTA button on page: %s", driver.current_url)
                return driver.current_url
        except Exception:
            continue

    return None


def _navigate_google_one(driver: webdriver.Chrome) -> Optional[str]:
    """
    Navigate to Google One and attempt to find the Gemini Pro offer link.

    Returns the payment/activation URL or None if not found.
    """
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(3)

            # Dismiss cookie/consent banners if present
            for selector in (
                '[aria-label="Accept all"]',
                'button[jsname="higCR"]',
                '[data-action="accept"]',
            ):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


def check_gemini_offer(email: str, password: str, totp_secret: str,
                       device: DeviceProfile) -> Optional[str]:
    """
    Main entry point.

    Logs into *email* / *password* with TOTP 2FA using *totp_secret*,
    uses the supplied *device* profile, navigates to Google One,
    and returns the Gemini Pro offer link (or None).

    Raises :class:`GoogleAutomationError` if the driver cannot be started or
    the login step fails with an error.
    """
    driver: Optional[webdriver.Chrome] = None
    try:
        logger.info("Starting WebDriver for session %s", device.session_id)
        driver = _build_driver(device)

        logged_in = _gmail_login(driver, email, password, totp_secret)
        if not logged_in:
            raise GoogleAutomationError(
                "Login failed – please check your credentials."
            )

        offer_link = _navigate_google_one(driver)
        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
