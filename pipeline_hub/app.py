"""
Pipeline Hub — GitHub Workflow Dashboard (Production)
=====================================================
A Flask app that aggregates GitHub Actions workflows from multiple repos,
lets you trigger builds, and monitor run status — all from one place.

Authentication Modes:
    1. OAuth (recommended) — users log in with their GitHub account
       Requires: GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, FLASK_SECRET_KEY
    2. PAT (legacy) — shared Personal Access Token for all API calls
       Requires: GITHUB_TOKEN

Other Environment Variables:
    GITHUB_ORG         — GitHub org/user to list repos from (optional)
    GITHUB_REPOS       — Comma-separated list of specific repos (optional)
    BASE_URL           — External URL of the app (e.g. https://pipeline-hub.example.com)
                          Required for OAuth behind a reverse proxy / Ingress.
    PROXY_URL          — Corporate HTTP proxy (e.g. http://proxy.company.com:8080)
                          Enables Kerberos (SPNEGO) proxy authentication.
    SSL_VERIFY         — Set to 'false' to disable SSL cert verification (default: true)
    PIPELINE_HUB_PORT  — Port to run on (default: 9090)

For local development without a GitHub token, use mock_app.py instead.
"""

from flask import Flask, render_template, jsonify, request, redirect, session, url_for
from functools import wraps
import os, json, base64, secrets, requests, time, threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

app = Flask(__name__)

# Support running behind a reverse proxy / Ingress
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.getenv('GITHUB_CLIENT_ID', '')
GITHUB_CLIENT_SECRET = os.getenv('GITHUB_CLIENT_SECRET', '')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
GITHUB_ORG = os.getenv('GITHUB_ORG', '')
GITHUB_REPOS = [r.strip() for r in os.getenv('GITHUB_REPOS', '').split(',') if r.strip()]
BASE_URL = os.getenv('BASE_URL', '').rstrip('/')  # e.g. https://pipeline-hub.example.com

# SSL verification — set to 'false' to disable (needed behind corporate TLS-intercepting proxies)
SSL_VERIFY = os.getenv('SSL_VERIFY', 'true').lower() not in ('false', '0', 'no')
if not SSL_VERIFY:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print('[Pipeline Hub] ⚠️  SSL verification DISABLED (SSL_VERIFY=false)')

# GitHub Enterprise support — set GITHUB_URL to your GHE instance
# e.g. https://github.yourcompany.com
GITHUB_URL = os.getenv('GITHUB_URL', 'https://github.com').rstrip('/')
if GITHUB_URL == 'https://github.com':
    GITHUB_API = 'https://api.github.com'
else:
    # GitHub Enterprise uses /api/v3 path on the same host
    GITHUB_API = f'{GITHUB_URL}/api/v3'

# ──────────────────────────────────────────────────────────────────────────────
# Proxy + Kerberos — create a shared requests.Session
# ──────────────────────────────────────────────────────────────────────────────
# If PROXY_URL is set (e.g. http://proxy.yourcompany.com:8080), the session
# will route all HTTPS traffic through that proxy and authenticate using
# Kerberos (SPNEGO / Negotiate), which is required in many corporate networks.
#
# Env vars:
#   PROXY_URL       — corporate proxy (e.g. http://proxy.yourcompany.com:8080)
#   SSL_VERIFY      — set to 'false' to disable cert verification (see above)
# ──────────────────────────────────────────────────────────────────────────────

PROXY_URL = os.getenv('PROXY_URL', '')

def _build_session():
    """Build a requests.Session with optional Kerberos proxy authentication."""
    s = requests.Session()
    s.verify = SSL_VERIFY

    if PROXY_URL:
        # Set proxy for both HTTP and HTTPS
        s.proxies = {
            'http': PROXY_URL,
            'https': PROXY_URL,
        }
        try:
            from requests_kerberos import HTTPKerberosAuth, OPTIONAL
            from requests.adapters import HTTPAdapter

            # Mount an adapter so every request goes through the session
            adapter = HTTPAdapter(max_retries=3)
            s.mount('https://', adapter)
            s.mount('http://', adapter)

            # Use Kerberos for proxy authentication (Negotiate / SPNEGO)
            s.auth = HTTPKerberosAuth(mutual_authentication=OPTIONAL)
            print(f'[Pipeline Hub] ✅ Kerberos proxy auth enabled — proxy: {PROXY_URL}')
        except ImportError:
            print(f'[Pipeline Hub] ⚠️  requests-kerberos not installed. Proxy set but Kerberos auth unavailable.')
            print(f'    Install it: pip install requests-kerberos')
    else:
        print('[Pipeline Hub] ℹ️  No PROXY_URL set — direct connections to GitHub.')

    return s

# Shared session used by all API calls
http = _build_session()

# Flask session secret — required for OAuth mode, auto-generated if not set
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# ──────────────────────────────────────────────────────────────────────────────
# In-memory TTL Cache — avoids repeated GitHub API calls
# ──────────────────────────────────────────────────────────────────────────────

_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = int(os.getenv('CACHE_TTL', '300'))  # 5 minutes default

def cache_get(key):
    """Get a value from cache if it exists and hasn't expired."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry['ts'] < CACHE_TTL:
            return entry['data']
    return None

def cache_set(key, data):
    """Store a value in cache with current timestamp."""
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}

def cache_clear(prefix=None):
    """Clear cache entries. If prefix given, only clear matching keys."""
    with _cache_lock:
        if prefix:
            keys_to_delete = [k for k in _cache if k.startswith(prefix)]
            for k in keys_to_delete:
                del _cache[k]
        else:
            _cache.clear()

# Determine auth mode
AUTH_MODE = 'oauth' if (GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET) else 'pat'

if AUTH_MODE == 'oauth':
    callback_url = f'{BASE_URL}/auth/callback' if BASE_URL else '(auto-detected)'
    print(f"[Pipeline Hub] OAuth mode — Client ID: {GITHUB_CLIENT_ID[:8]}...")
    print(f"    GitHub URL: {GITHUB_URL}")
    print(f"    API base:   {GITHUB_API}")
    print(f"    Callback URL: {callback_url}")
    print(f"    Users will log in with their GitHub accounts.")
    if not BASE_URL:
        print(f"    ⚠️  No BASE_URL set. Set it for production: BASE_URL=https://your-domain.com")
elif GITHUB_TOKEN:
    print(f"[Pipeline Hub] PAT mode — using shared token for all API calls")
    print(f"    org: {GITHUB_ORG or 'auto'}, repos filter: {len(GITHUB_REPOS) or 'all'}")
else:
    print("⚠️  [Pipeline Hub] No auth configured. API calls will fail.")
    print("    Set GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET for OAuth mode")
    print("    Or set GITHUB_TOKEN for PAT mode")
    print("    For local testing, use: python mock_app.py")


# ──────────────────────────────────────────────────────────────────────────────
# Auth Helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_token():
    """Get the GitHub token for the current request.
    
    In OAuth mode: returns the logged-in user's OAuth token from the session.
    In PAT mode: returns the shared GITHUB_TOKEN.
    Returns empty string if no token is available.
    """
    if AUTH_MODE == 'oauth':
        return session.get('github_token', '')
    return GITHUB_TOKEN


def is_authenticated():
    """Check if the current user is authenticated."""
    if AUTH_MODE == 'oauth':
        return bool(session.get('github_token'))
    return bool(GITHUB_TOKEN)


def login_required(f):
    """Decorator to require authentication for API routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated', 'auth_required': True}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _get_callback_url():
    """Get the OAuth callback URL.
    
    Uses BASE_URL if set (required for K8s/Ingress/proxy deployments).
    Falls back to url_for() for local development.
    """
    if BASE_URL:
        return f'{BASE_URL}/auth/callback'
    return url_for('auth_callback', _external=True)


# ──────────────────────────────────────────────────────────────────────────────
# GitHub API Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _headers():
    token = get_token()
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'PipelineHub/1.0'
    }


def _github_get(path, params=None):
    """GET request to GitHub API with error handling."""
    url = f'{GITHUB_API}{path}'
    try:
        r = http.get(url, headers=_headers(), params=params, timeout=15)
        # Log response details for debugging
        scopes = r.headers.get('X-OAuth-Scopes', 'N/A')
        print(f"[GitHub API] GET {path} → {r.status_code} "
              f"(scopes: {scopes}, size: {len(r.content)} bytes)")
        r.raise_for_status()
        data = r.json()
        # Warn if we got a dict instead of a list for endpoints that should return lists
        if path.endswith('/repos') and isinstance(data, dict):
            print(f"[GitHub API] ⚠️  Expected list for {path} but got dict: {str(data)[:300]}")
        return data
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else 'no response'
        print(f"[GitHub API] ❌ HTTP {e.response.status_code} for {path}")
        print(f"    Response: {body}")
        print(f"    Scopes: {e.response.headers.get('X-OAuth-Scopes', 'N/A')}")
        raise
    except Exception as e:
        print(f"[GitHub API] ❌ Error for {path}: {e}")
        raise


def _github_post(path, data=None):
    """POST request to GitHub API."""
    url = f'{GITHUB_API}{path}'
    try:
        r = http.post(url, headers=_headers(), json=data, timeout=15)
        return r
    except Exception as e:
        print(f"[GitHub API] POST error for {path}: {e}")
        raise


def _format_duration(seconds):
    """Convert seconds to 'Xm XXs' format."""
    if not seconds or seconds <= 0:
        return "--"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _time_ago(iso_str):
    """Convert ISO timestamp to '5 min ago' format."""
    if not iso_str:
        return "Never"
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        diff = now - dt
        minutes = int(diff.total_seconds() / 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        days = hours // 24
        if days < 7:
            return f"{days} day{'s' if days > 1 else ''} ago"
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''} ago"
    except Exception:
        return iso_str


def _parse_workflow_inputs(content_b64):
    """Parse workflow_dispatch inputs from base64-encoded workflow YAML."""
    inputs = []
    try:
        import yaml
        raw = base64.b64decode(content_b64).decode('utf-8')
        wf = yaml.safe_load(raw)
        if not wf or not isinstance(wf, dict):
            return inputs
        on_section = wf.get('on', wf.get(True, {}))
        if isinstance(on_section, dict):
            dispatch = on_section.get('workflow_dispatch', {})
            if isinstance(dispatch, dict) and dispatch.get('inputs'):
                for name, config in dispatch['inputs'].items():
                    inp = {
                        'name': name,
                        'type': config.get('type', 'string'),
                        'default': str(config.get('default', '')),
                    }
                    if config.get('options'):
                        inp['options'] = config['options']
                    inputs.append(inp)
    except Exception as e:
        print(f"[parse_inputs] Error: {e}")
    return inputs


# ──────────────────────────────────────────────────────────────────────────────
# OAuth Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/login')
def login():
    """Redirect the user to GitHub's OAuth authorization page."""
    if AUTH_MODE != 'oauth':
        return redirect('/')

    # Generate a random state to prevent CSRF
    state = secrets.token_hex(16)
    session['oauth_state'] = state

    params = {
        'client_id': GITHUB_CLIENT_ID,
        'redirect_uri': _get_callback_url(),
        'scope': 'repo workflow read:org',
        'state': state,
    }
    github_auth_url = f'{GITHUB_URL}/login/oauth/authorize?{urlencode(params)}'
    return redirect(github_auth_url)


@app.route('/auth/test')
def auth_test():
    """Simple test endpoint to verify /auth/* routing works."""
    print('[DEBUG] /auth/test was reached!')
    return jsonify({'status': 'ok', 'message': '/auth/test route is reachable'})


@app.route('/auth/callback')
def auth_callback():
    """Handle the OAuth callback from GitHub."""
    print(f'[DEBUG] /auth/callback HIT! args={dict(request.args)}')

    if AUTH_MODE != 'oauth':
        return redirect('/')

    # Verify state to prevent CSRF
    state = request.args.get('state', '')
    if state != session.get('oauth_state'):
        return jsonify({'error': 'Invalid OAuth state. Please try logging in again.'}), 403

    code = request.args.get('code', '')
    if not code:
        return jsonify({'error': 'No authorization code received from GitHub.'}), 400

    # Exchange the code for an access token.
    #
    # We use a SEPARATE session here instead of the shared `http` session
    # because `http` has HTTPAdapter(max_retries=3) which conflicts with
    # HTTPKerberosAuth's 407 proxy-auth retry — the adapter retries the
    # raw connection while Kerberos tries to resend with auth headers,
    # corrupting the CONNECT tunnel (errno 104, connection reset).
    #
    # This OAuth session has proxy + Kerberos (for proxy auth) but NO
    # retry adapter, so Kerberos can cleanly handle the 407 handshake.
    try:
        token_url = f'{GITHUB_URL}/login/oauth/access_token'
        token_payload = {
            'client_id': GITHUB_CLIENT_ID,
            'client_secret': GITHUB_CLIENT_SECRET,
            'code': code,
            'redirect_uri': _get_callback_url(),
        }

        # Build a dedicated session for OAuth token exchange
        oauth_http = requests.Session()
        oauth_http.verify = SSL_VERIFY
        if PROXY_URL:
            oauth_http.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
            try:
                from requests_kerberos import HTTPKerberosAuth, OPTIONAL
                oauth_http.auth = HTTPKerberosAuth(
                    mutual_authentication=OPTIONAL,
                    force_preemptive=False,
                )
            except ImportError:
                pass

        print(f'[OAuth] Exchanging code for token via {token_url} (proxy: {PROXY_URL or "none"})')
        token_response = oauth_http.post(
            token_url,
            headers={'Accept': 'application/json'},
            data=token_payload,
            timeout=30,
        )
        oauth_http.close()
        token_data = token_response.json()

        if 'access_token' not in token_data:
            error = token_data.get('error_description', token_data.get('error', 'Unknown error'))
            print(f"[OAuth] Token exchange failed: {error}")
            return jsonify({'error': f'OAuth failed: {error}'}), 400

        # Store the token in the session
        access_token = token_data['access_token']
        session['github_token'] = access_token

        # Fetch user info and store in session
        user_response = http.get(
            f'{GITHUB_API}/user',
            headers={
                'Authorization': f'token {access_token}',
                'Accept': 'application/vnd.github.v3+json',
            },
            timeout=10,
        )
        if user_response.status_code == 200:
            user_data = user_response.json()
            session['github_user'] = {
                'login': user_data.get('login', ''),
                'name': user_data.get('name', ''),
                'avatar_url': user_data.get('avatar_url', ''),
            }

        # Clear the OAuth state
        session.pop('oauth_state', None)

        print(f"[OAuth] User {session.get('github_user', {}).get('login', 'unknown')} logged in successfully")
        return redirect('/')

    except Exception as e:
        print(f"[OAuth] Error during token exchange: {e}")
        return jsonify({'error': f'OAuth error: {str(e)}'}), 500


@app.route('/logout')
def logout():
    """Clear the session and redirect to home."""
    user = session.get('github_user', {}).get('login', 'unknown')
    session.clear()
    print(f"[OAuth] User {user} logged out")
    return redirect('/')


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config')
def get_config():
    user = session.get('github_user', None)
    return jsonify({
        'mode': 'live',
        'auth_mode': AUTH_MODE,
        'logged_in': is_authenticated(),
        'user': user,
        'org': GITHUB_ORG or 'auto',
        'repos_filter': GITHUB_REPOS,
        'token_set': is_authenticated(),
    })


@app.route('/api/user')
def get_user():
    """Return the current authenticated user info."""
    if not is_authenticated():
        return jsonify({
            'logged_in': False,
            'auth_mode': AUTH_MODE,
        })

    user = session.get('github_user', None)
    if user:
        return jsonify({
            'logged_in': True,
            'auth_mode': AUTH_MODE,
            **user,
        })

    # PAT mode — fetch user from GitHub API
    if AUTH_MODE == 'pat':
        try:
            data = _github_get('/user')
            return jsonify({
                'logged_in': True,
                'auth_mode': AUTH_MODE,
                'login': data.get('login', ''),
                'name': data.get('name', ''),
                'avatar_url': data.get('avatar_url', ''),
            })
        except Exception:
            return jsonify({
                'logged_in': True,
                'auth_mode': AUTH_MODE,
                'login': 'service-account',
                'name': 'Service Account',
                'avatar_url': '',
            })

    return jsonify({'logged_in': False, 'auth_mode': AUTH_MODE})


@app.route('/api/repos')
@login_required
def list_repos():
    try:
        # Check cache first — avoids re-fetching on every page load / stats call
        cache_key = f'repos:{get_token()[:8] if get_token() else "anon"}'
        cached = cache_get(cache_key)
        if cached is not None:
            print(f"[list_repos] ⚡ Returning {len(cached)} repos from cache")
            return jsonify(cached)

        repos = []

        if GITHUB_REPOS:
            # Mode 1: Specific repos configured via GITHUB_REPOS env var
            print(f"[list_repos] Mode 1: Fetching {len(GITHUB_REPOS)} specific repos")
            for repo_name in GITHUB_REPOS:
                try:
                    path = f'/repos/{repo_name}' if '/' in repo_name else f'/repos/{GITHUB_ORG}/{repo_name}'
                    data = _github_get(path)
                    if data.get('archived'):
                        print(f"[list_repos] Skipping archived repo: {repo_name}")
                        continue
                    repos.append({
                        'name': data['name'],
                        'full_name': data['full_name'],
                        'language': data.get('language') or 'Unknown',
                        'default_branch': data.get('default_branch') or 'main',
                        'visibility': data.get('visibility', 'private'),
                        'archived': data.get('archived', False),
                    })
                except Exception as e:
                    print(f"[list_repos] ❌ Skipping {repo_name}: {e}")

        elif GITHUB_ORG:
            # Mode 2: All repos from an org
            print(f"[list_repos] Mode 2: Listing repos from org '{GITHUB_ORG}'")
            try:
                page = 1
                while True:
                    data = _github_get(f'/orgs/{GITHUB_ORG}/repos', {
                        'per_page': 100, 'page': page, 'sort': 'updated', 'type': 'all'
                    })
                    if not data or not isinstance(data, list):
                        break
                    for r in data:
                        if r.get('archived'):
                            print(f"[list_repos] Skipping archived repo: {r.get('name', '?')}")
                            continue
                        repos.append({
                            'name': r['name'],
                            'full_name': r['full_name'],
                            'language': r.get('language') or 'Unknown',
                            'default_branch': r.get('default_branch') or 'main',
                            'visibility': r.get('visibility', 'private'),
                            'archived': r.get('archived', False),
                        })
                    if len(data) < 100:
                        break
                    page += 1
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 'unknown'
                print(f"[list_repos] ⚠️  Org listing failed (HTTP {status}). "
                      f"The OAuth app may not be approved for org '{GITHUB_ORG}'.")
                print(f"    → Ask an org admin to approve the app at: "
                      f"{GITHUB_URL}/organizations/{GITHUB_ORG}/settings/oauth_application_policy")

            # Fallback: if org listing returned zero repos, try /user/repos filtered by org
            if not repos:
                print(f"[list_repos] ⚠️  Org listing returned 0 repos — trying /user/repos fallback...")
                try:
                    all_user_repos = _github_get('/user/repos', {
                        'per_page': 100, 'sort': 'updated',
                        'affiliation': 'owner,collaborator,organization_member'
                    })
                    for r in (all_user_repos if isinstance(all_user_repos, list) else []):
                        if r.get('archived'):
                            print(f"[list_repos] Skipping archived repo: {r.get('name', '?')}")
                            continue
                        if r.get('owner', {}).get('login', '').lower() == GITHUB_ORG.lower():
                            repos.append({
                                'name': r['name'],
                                'full_name': r['full_name'],
                                'language': r.get('language') or 'Unknown',
                                'default_branch': r.get('default_branch') or 'main',
                                'visibility': r.get('visibility', 'private'),
                                'archived': r.get('archived', False),
                            })
                    print(f"[list_repos] Fallback found {len(repos)} repos from org '{GITHUB_ORG}' via /user/repos")
                except Exception as fallback_err:
                    print(f"[list_repos] Fallback also failed: {fallback_err}")

        else:
            # Mode 3: All repos accessible by the token owner
            print(f"[list_repos] Mode 3: Listing all repos accessible by token")
            data = _github_get('/user/repos', {
                'per_page': 100, 'sort': 'updated',
                'affiliation': 'owner,collaborator,organization_member'
            })
            for r in (data if isinstance(data, list) else []):
                if r.get('archived'):
                    print(f"[list_repos] Skipping archived repo: {r.get('name', '?')}")
                    continue
                repos.append({
                    'name': r['name'],
                    'full_name': r['full_name'],
                    'language': r.get('language') or 'Unknown',
                    'default_branch': r.get('default_branch') or 'main',
                    'visibility': r.get('visibility', 'private'),
                    'archived': r.get('archived', False),
                })

        # Final safety filter: remove any archived repos that slipped through
        before_count = len(repos)
        repos = [r for r in repos if not r.get('archived', False)]
        if before_count != len(repos):
            print(f"[list_repos] ⚠ Removed {before_count - len(repos)} archived repos in final filter")

        print(f"[list_repos] ✅ Returning {len(repos)} repos (non-archived)")
        repos.sort(key=lambda r: r['name'])

        # Cache the result
        cache_set(cache_key, repos)

        return jsonify(repos)

    except Exception as e:
        print(f"[list_repos] ❌ Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/debug/auth')
@login_required
def debug_auth():
    """Diagnostic endpoint to check token permissions and org access.
    
    Call this from a browser to see:
    - Token scopes granted by GitHub
    - Whether the OAuth app is approved for the org
    - Which orgs the user belongs to
    - How many repos are accessible
    """
    result = {
        'auth_mode': AUTH_MODE,
        'github_url': GITHUB_URL,
        'github_api': GITHUB_API,
        'configured_org': GITHUB_ORG or '(not set)',
        'configured_repos': GITHUB_REPOS or '(not set)',
        'checks': [],
    }

    token = get_token()
    if not token:
        result['checks'].append({'name': 'Token', 'status': '❌', 'detail': 'No token available'})
        return jsonify(result)

    # Check 1: Token validity and scopes
    try:
        r = http.get(f'{GITHUB_API}/user', headers=_headers(), timeout=10)
        result['user'] = r.json().get('login', 'unknown')
        scopes = r.headers.get('X-OAuth-Scopes', 'unknown')
        result['token_scopes'] = scopes
        result['checks'].append({
            'name': 'Token Valid',
            'status': '✅',
            'detail': f'Logged in as {result["user"]}. Scopes: {scopes}'
        })

        # Warn if scopes are missing
        if 'repo' not in scopes and 'read' not in scopes:
            result['checks'].append({
                'name': 'Scope Warning',
                'status': '⚠️',
                'detail': f'Token may be missing "repo" scope. Current scopes: {scopes}'
            })
    except Exception as e:
        result['checks'].append({'name': 'Token Valid', 'status': '❌', 'detail': str(e)})
        return jsonify(result)

    # Check 2: User's org memberships
    try:
        orgs = _github_get('/user/orgs')
        org_names = [o.get('login', '') for o in orgs]
        result['user_orgs'] = org_names
        result['checks'].append({
            'name': 'Org Memberships',
            'status': '✅' if org_names else '⚠️',
            'detail': f'Member of: {org_names}' if org_names else 'Not a member of any org via this token'
        })
    except Exception as e:
        result['checks'].append({'name': 'Org Memberships', 'status': '❌', 'detail': str(e)})
        org_names = []

    # Check 3: Org access (if GITHUB_ORG is set)
    if GITHUB_ORG:
        if GITHUB_ORG.lower() in [o.lower() for o in org_names]:
            result['checks'].append({
                'name': f'Org "{GITHUB_ORG}" Membership',
                'status': '✅',
                'detail': f'User is a member of {GITHUB_ORG}'
            })
        else:
            result['checks'].append({
                'name': f'Org "{GITHUB_ORG}" Membership',
                'status': '❌',
                'detail': f'User is NOT a member of "{GITHUB_ORG}" (or OAuth app not approved for this org). '
                          f'Ask an org admin to approve at: {GITHUB_URL}/organizations/{GITHUB_ORG}/settings/oauth_application_policy'
            })

        # Try listing org repos directly
        try:
            test_repos = _github_get(f'/orgs/{GITHUB_ORG}/repos', {'per_page': 5})
            count = len(test_repos) if isinstance(test_repos, list) else 0
            result['checks'].append({
                'name': f'Org "{GITHUB_ORG}" Repo Access',
                'status': '✅' if count > 0 else '❌',
                'detail': f'Can see {count}+ repos from org' if count > 0
                          else f'Cannot list repos from org "{GITHUB_ORG}". '
                               f'The OAuth app likely needs org admin approval.'
            })
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 'unknown'
            result['checks'].append({
                'name': f'Org "{GITHUB_ORG}" Repo Access',
                'status': '❌',
                'detail': f'HTTP {status} — OAuth app not authorized for this org. '
                          f'Fix: {GITHUB_URL}/organizations/{GITHUB_ORG}/settings/oauth_application_policy'
            })

    # Check 4: User repos count (fallback)
    try:
        user_repos = _github_get('/user/repos', {'per_page': 5, 'affiliation': 'owner,collaborator,organization_member'})
        count = len(user_repos) if isinstance(user_repos, list) else 0
        result['checks'].append({
            'name': 'User Repos (/user/repos)',
            'status': '✅' if count > 0 else '⚠️',
            'detail': f'Can see {count}+ repos via /user/repos' if count > 0
                      else 'No repos accessible via /user/repos'
        })
    except Exception as e:
        result['checks'].append({'name': 'User Repos', 'status': '❌', 'detail': str(e)})

    return jsonify(result)


@app.route('/api/repos/<owner>/<repo>/workflows')
@login_required
def list_workflows(owner, repo):
    """List workflows for a repo — optimized with caching and batch run fetching.
    
    Optimizations vs original:
    - Cache results for CACHE_TTL seconds
    - Fetch recent runs in ONE batch call instead of per-workflow
    - Defer workflow YAML parsing to /api/repos/<owner>/<repo>/workflows/<id>/inputs
    """
    try:
        cache_key = f'workflows:{owner}/{repo}'
        cached = cache_get(cache_key)
        if cached is not None:
            print(f"[list_workflows] ⚡ Returning {len(cached)} workflows for {owner}/{repo} from cache")
            return jsonify(cached)

        t_start = time.time()

        # 1. Fetch workflow list (1 API call)
        data = _github_get(f'/repos/{owner}/{repo}/actions/workflows')
        active_workflows = [w for w in data.get('workflows', []) if w.get('state') == 'active']

        # 2. Fetch recent runs in ONE batch call instead of per-workflow (1 API call)
        #    This replaces N individual /runs calls with a single call
        last_run_by_workflow = {}
        try:
            runs_data = _github_get(f'/repos/{owner}/{repo}/actions/runs', {'per_page': 50})
            for run in runs_data.get('workflow_runs', []):
                wf_id = run.get('workflow_id')
                if wf_id and wf_id not in last_run_by_workflow:
                    last_run_by_workflow[wf_id] = run  # Keep only the most recent run per workflow
        except Exception as e:
            print(f"[list_workflows] ⚠️  Failed to batch-fetch runs: {e}")

        # 3. Build workflow list (0 additional API calls)
        workflows = []
        for w in active_workflows:
            last_conclusion = None
            last_run_ago = "Never"
            last_run_by = "--"
            duration = "--"
            branch = ""

            run = last_run_by_workflow.get(w['id'])
            if run:
                last_conclusion = run.get('conclusion') or run.get('status')
                last_run_ago = _time_ago(run.get('created_at'))
                last_run_by = (run.get('actor') or {}).get('login', '--')
                branch = run.get('head_branch', '')
                if run.get('created_at') and run.get('updated_at'):
                    try:
                        start = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
                        end = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
                        duration = _format_duration((end - start).total_seconds())
                    except Exception:
                        pass

            workflows.append({
                'id': w['id'],
                'name': w['name'],
                'file': w['path'].split('/')[-1],
                'path': w['path'],
                'state': w['state'],
                'last_conclusion': last_conclusion,
                'last_run_ago': last_run_ago,
                'duration': duration,
                'last_run_by': last_run_by,
                'branch': branch,
                'dispatch_inputs': [],  # Loaded on-demand via /inputs endpoint
            })

        elapsed = time.time() - t_start
        print(f"[list_workflows] ✅ {owner}/{repo}: {len(workflows)} workflows in {elapsed:.1f}s (2 API calls)")

        cache_set(cache_key, workflows)
        return jsonify(workflows)

    except Exception as e:
        print(f"[list_workflows] ❌ Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/workflows/<int:workflow_id>/inputs')
@login_required
def get_workflow_inputs(owner, repo, workflow_id):
    """Fetch workflow_dispatch inputs on-demand (only when user clicks Run).
    
    This is deferred from the workflow listing to avoid N extra API calls
    (one per workflow) just to parse YAML files that are rarely needed.
    """
    try:
        cache_key = f'inputs:{owner}/{repo}/{workflow_id}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        # Find the workflow to get its file path
        data = _github_get(f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}')
        wf_path = data.get('path', '')
        if not wf_path:
            return jsonify([])

        # Fetch the YAML file and parse inputs
        file_data = _github_get(f'/repos/{owner}/{repo}/contents/{wf_path}')
        inputs = []
        if file_data.get('content'):
            inputs = _parse_workflow_inputs(file_data['content'])

        cache_set(cache_key, inputs)
        return jsonify(inputs)

    except Exception as e:
        print(f"[get_inputs] Error for workflow {workflow_id}: {e}")
        return jsonify([])

@app.route('/api/repos/<owner>/<repo>/branches')
@login_required
def list_branches(owner, repo):
    """Fetch branches for a repo — priority branches first, then up to 50 others.

    Strategy:
      1. Scan up to 5 API pages (500 branches)
      2. For large repos, individually check each env branch by name
         (in case they're beyond page 5)
      3. Split into PRIORITY and REGULAR:
         - Priority: default branch, main, master, dev, uat, staging, prod,
           production, pprod, preprod, RELEASE_*, release/*, hotfix/*
         - Regular: everything else (feature/*, bugfix/*, etc.)
      4. Priority sorted by importance (default first)
      5. Regular: last 50 alphabetically (higher numbers = likely newer)
      6. No date filtering — consistent results across all repos
    """
    try:
        cache_key = f'branches:{owner}/{repo}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        # Priority branch names — always included if they exist
        PRIORITY_NAMES = {
            'main', 'master', 'develop', 'dev', 'uat', 'staging',
            'prod', 'production', 'pprod', 'preprod',
        }

        # Include the repo's default branch (whatever it's called)
        default_branch = request.args.get('default', '').strip()
        if default_branch:
            PRIORITY_NAMES.add(default_branch.lower())

        # Prefixes that are always treated as priority
        PRIORITY_PREFIXES = ('release_', 'release/', 'hotfix/', 'hotfix-')

        # ── Step 1: Scan up to 5 pages (500 branches) ──
        all_branch_names = []
        MAX_PAGES = 5
        for page in range(1, MAX_PAGES + 1):
            data = _github_get(f'/repos/{owner}/{repo}/branches',
                               {'per_page': 100, 'page': page})
            if not data or not isinstance(data, list):
                break
            all_branch_names.extend([b.get('name', '') for b in data])
            if len(data) < 100:
                break  # No more pages

        scanned_set = {b.lower() for b in all_branch_names}
        has_many = len(all_branch_names) >= MAX_PAGES * 100

        # ── Step 2: For large repos, check missing priority branches individually ──
        injected = []
        if len(all_branch_names) >= 200:
            missing = [n for n in PRIORITY_NAMES if n not in scanned_set]
            for branch_name in missing:
                try:
                    bdata = _github_get(f'/repos/{owner}/{repo}/branches/{branch_name}')
                    if bdata and isinstance(bdata, dict) and bdata.get('name'):
                        all_branch_names.append(bdata['name'])
                        injected.append(bdata['name'])
                except Exception:
                    pass  # Branch doesn't exist in this repo

            if injected:
                print(f"[list_branches] {owner}/{repo}: injected {len(injected)} "
                      f"priority branches not in scan: {injected}")

        # ── Step 3: Split into priority and regular ──
        priority = []
        regular = []
        seen = set()
        for b in all_branch_names:
            bl = b.lower()
            if bl in seen:
                continue
            seen.add(bl)
            if bl in PRIORITY_NAMES or bl.startswith(PRIORITY_PREFIXES):
                priority.append(b)
            else:
                regular.append(b)

        # ── Step 4: Sort priority by importance (default branch always first) ──
        priority_order = {
            'main': 0, 'master': 1, 'develop': 2, 'dev': 3,
            'uat': 4, 'staging': 5, 'pprod': 6, 'preprod': 7,
            'prod': 8, 'production': 9,
        }
        def priority_sort_key(b):
            if default_branch and b.lower() == default_branch.lower():
                return (-1, '')
            base = priority_order.get(b.lower(), 20)
            # RELEASE and hotfix after named envs but before regular
            if b.lower().startswith(('release_', 'release/')):
                base = 11
            elif b.lower().startswith(('hotfix/', 'hotfix-')):
                base = 12
            return (base, b.lower())

        priority.sort(key=priority_sort_key)

        # ── Step 5: Cap regular branches at 50 ──
        # Take the LAST 50 alphabetically — higher ticket numbers are typically newer
        # (e.g., feature/JIRA-300 is newer than feature/JIRA-100)
        MAX_REGULAR = 50
        if len(regular) > MAX_REGULAR:
            regular = regular[-MAX_REGULAR:]

        # ── Step 6: Build result ──
        result = priority + regular

        # Safety: if default branch STILL missing (edge case), inject it
        if default_branch:
            if not any(b.lower() == default_branch.lower() for b in result):
                result.insert(0, default_branch)
                print(f"[list_branches] ⚠ Default branch '{default_branch}' injected at top")

        print(f"[list_branches] {owner}/{repo}: scanned {len(all_branch_names)} branches"
              f"{' (large repo)' if has_many else ''} → "
              f"{len(priority)} priority + {len(regular)} regular = {len(result)} total")
        if priority:
            print(f"[list_branches]   priority: {priority}")

        cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        print(f"[list_branches] Error for {owner}/{repo}: {e}")
        return jsonify([])


@app.route('/api/repos/<owner>/<repo>/runs')
@login_required
def list_runs(owner, repo):
    try:
        data = _github_get(f'/repos/{owner}/{repo}/actions/runs', {'per_page': 20})
        runs = []
        for run in data.get('workflow_runs', []):
            duration = "--"
            if run.get('status') == 'completed' and run.get('created_at') and run.get('updated_at'):
                try:
                    start = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
                    duration = _format_duration((end - start).total_seconds())
                except Exception:
                    pass

            runs.append({
                'id': run['id'],
                'run_number': run['run_number'],
                'name': run['name'],
                'workflow_file': (run.get('path') or '').split('/')[-1],
                'status': run['status'],
                'conclusion': run.get('conclusion'),
                'branch': run.get('head_branch', ''),
                'triggered_by': (run.get('actor') or {}).get('login', '--'),
                'created_at': run.get('created_at', ''),
                'duration': duration if run['status'] == 'completed' else 'running...',
                'url': run.get('html_url', ''),
            })

        return jsonify(runs)

    except Exception as e:
        print(f"[list_runs] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/workflows/<int:workflow_id>/run', methods=['POST'])
@login_required
def trigger_workflow(owner, repo, workflow_id):
    try:
        data = request.json or {}
        branch = data.get('branch', 'main')
        inputs = data.get('inputs', {})

        response = _github_post(
            f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
            {'ref': branch, 'inputs': inputs}
        )

        if response.status_code == 204:
            triggered_by = session.get('github_user', {}).get('login', 'unknown')
            print(f"[trigger] {triggered_by} triggered workflow {workflow_id} on {owner}/{repo}@{branch}")
            return jsonify({'status': 'triggered', 'message': f'✅ Workflow triggered on {branch}'})
        elif response.status_code == 422:
            error_detail = ""
            try:
                error_detail = response.json().get('message', '')
            except Exception:
                error_detail = response.text[:200]
            return jsonify({'error': f'Cannot trigger: {error_detail}. Ensure workflow has workflow_dispatch trigger and branch exists.'}), 422
        elif response.status_code == 403:
            return jsonify({'error': 'Permission denied. Your GitHub account may not have write access to this repo.'}), 403
        elif response.status_code == 404:
            return jsonify({'error': 'Workflow or repo not found. Check permissions.'}), 404
        else:
            return jsonify({'error': f'GitHub returned {response.status_code}: {response.text[:200]}'}), response.status_code

    except Exception as e:
        print(f"[trigger_workflow] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/runs/<int:run_id>/rerun', methods=['POST'])
@login_required
def rerun_all_jobs(owner, repo, run_id):
    """Re-run ALL jobs in a workflow run."""
    try:
        response = _github_post(f'/repos/{owner}/{repo}/actions/runs/{run_id}/rerun', {})
        triggered_by = session.get('github_user', {}).get('login', 'unknown')

        if response.status_code == 201:
            print(f"[rerun] {triggered_by} re-ran all jobs for run #{run_id} on {owner}/{repo}")
            # Invalidate workflow cache so next refresh shows updated status
            cache_clear(f'workflows:{owner}/{repo}')
            return jsonify({'status': 'rerun', 'message': f'✅ Re-running all jobs for run #{run_id}'})
        elif response.status_code == 403:
            return jsonify({'error': 'Permission denied. You need write access to re-run workflows.'}), 403
        elif response.status_code == 409:
            return jsonify({'error': 'Cannot re-run: the workflow run is still in progress.'}), 409
        else:
            detail = ''
            try:
                detail = response.json().get('message', response.text[:200])
            except Exception:
                detail = response.text[:200]
            return jsonify({'error': f'GitHub returned {response.status_code}: {detail}'}), response.status_code

    except Exception as e:
        print(f"[rerun] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/runs/<int:run_id>/rerun-failed', methods=['POST'])
@login_required
def rerun_failed_jobs(owner, repo, run_id):
    """Re-run only FAILED jobs in a workflow run.
    
    This is especially useful for Terraform workflows where a transient
    provider error shouldn't force a full re-plan.
    """
    try:
        response = _github_post(f'/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs', {})
        triggered_by = session.get('github_user', {}).get('login', 'unknown')

        if response.status_code == 201:
            print(f"[rerun-failed] {triggered_by} re-ran failed jobs for run #{run_id} on {owner}/{repo}")
            cache_clear(f'workflows:{owner}/{repo}')
            return jsonify({'status': 'rerun', 'message': f'✅ Re-running failed jobs for run #{run_id}'})
        elif response.status_code == 403:
            return jsonify({'error': 'Permission denied. You need write access to re-run workflows.'}), 403
        elif response.status_code == 409:
            return jsonify({'error': 'Cannot re-run: the workflow run is still in progress.'}), 409
        else:
            detail = ''
            try:
                detail = response.json().get('message', response.text[:200])
            except Exception:
                detail = response.text[:200]
            return jsonify({'error': f'GitHub returned {response.status_code}: {detail}'}), response.status_code

    except Exception as e:
        print(f"[rerun-failed] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats')
@login_required
def global_stats():
    """Smart stats using batch /actions/runs calls.
    
    Uses 1 API call per repo (max 15 repos) to get recent runs,
    then deduces per-workflow latest status from the batch results.
    Much cheaper than the old per-workflow approach (~15 calls vs 200+).
    """
    try:
        # Check cache first
        cache_key = f'stats:{get_token()[:8] if get_token() else "anon"}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        repos_resp = list_repos()
        repos = repos_resp.get_json()
        if isinstance(repos, dict) and 'error' in repos:
            return jsonify(repos), 500

        repo_list = repos if isinstance(repos, list) else []
        total_repos = len(repo_list)
        max_scan = min(total_repos, 15)  # Limit to 15 repos

        total_workflows = 0
        passing = 0
        failing = 0

        for repo in repo_list[:max_scan]:
            owner, name = repo['full_name'].split('/')
            try:
                # 1 API call per repo — get recent runs and deduce workflow status
                runs_data = _github_get(f'/repos/{owner}/{name}/actions/runs', {'per_page': 50})
                
                # Group by workflow_id, keep only the latest run per workflow
                latest_by_wf = {}
                for run in runs_data.get('workflow_runs', []):
                    wf_id = run.get('workflow_id')
                    if wf_id and wf_id not in latest_by_wf:
                        latest_by_wf[wf_id] = run

                for wf_id, run in latest_by_wf.items():
                    total_workflows += 1
                    conclusion = run.get('conclusion')
                    if conclusion == 'success':
                        passing += 1
                    elif conclusion == 'failure':
                        failing += 1
            except Exception as e:
                print(f"[stats] Error scanning {repo['full_name']}: {e}")

        result = {
            'total_repos': total_repos,
            'total_workflows': total_workflows,
            'passing': passing,
            'failing': failing,
            'success_rate': round(passing / total_workflows * 100, 1) if total_workflows else 0,
            'repos_scanned': max_scan,
        }

        print(f"[stats] ✅ Scanned {max_scan} repos: {total_workflows} workflows, "
              f"{passing} passing, {failing} failing")

        cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        print(f"[stats] ❌ Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats/full')
@login_required
def global_stats_full():
    """Full stats with per-workflow status checks.
    
    This is expensive (many API calls) — only call from a background
    task or when explicitly requested by the user.
    Limits to 10 repos max to avoid timeouts.
    """
    try:
        repos_resp = list_repos()
        repos = repos_resp.get_json()
        if isinstance(repos, dict) and 'error' in repos:
            return jsonify(repos), 500

        total_workflows = 0
        passing = 0
        failing = 0
        max_repos = min(len(repos) if isinstance(repos, list) else 0, 10)  # Hard limit

        print(f"[stats/full] Scanning {max_repos} repos (of {len(repos) if isinstance(repos, list) else 0} total)...")

        for repo in (repos[:max_repos] if isinstance(repos, list) else []):
            owner, name = repo['full_name'].split('/')
            try:
                data = _github_get(f'/repos/{owner}/{name}/actions/workflows', {'per_page': 100})
                for w in data.get('workflows', []):
                    if w.get('state') != 'active':
                        continue
                    total_workflows += 1
                    try:
                        runs = _github_get(
                            f'/repos/{owner}/{name}/actions/workflows/{w["id"]}/runs',
                            {'per_page': 1}
                        )
                        if runs.get('workflow_runs'):
                            conclusion = runs['workflow_runs'][0].get('conclusion')
                            if conclusion == 'success':
                                passing += 1
                            elif conclusion == 'failure':
                                failing += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"[stats/full] Error scanning {repo['full_name']}: {e}")

        print(f"[stats/full] Done: {total_workflows} workflows, {passing} passing, {failing} failing")

        return jsonify({
            'total_repos': len(repos) if isinstance(repos, list) else 0,
            'total_workflows': total_workflows,
            'passing': passing,
            'failing': failing,
            'success_rate': round(passing / total_workflows * 100, 1) if total_workflows else 0,
            'repos_scanned': max_repos,
        })

    except Exception as e:
        print(f"[stats/full] ❌ Error: {e}")
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Tier 1 Feature Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/api/repos/<owner>/<repo>/runs/<int:run_id>/jobs')
@login_required
def list_run_jobs(owner, repo, run_id):
    """Fetch jobs (with steps) for a specific workflow run."""
    try:
        cache_key = f'jobs:{owner}/{repo}/{run_id}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        data = _github_get(f'/repos/{owner}/{repo}/actions/runs/{run_id}/jobs')
        jobs = []
        for j in data.get('jobs', []):
            duration = "--"
            if j.get('started_at') and j.get('completed_at'):
                try:
                    start = datetime.fromisoformat(j['started_at'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(j['completed_at'].replace('Z', '+00:00'))
                    duration = _format_duration((end - start).total_seconds())
                except Exception:
                    pass

            steps = []
            for s in j.get('steps', []):
                step_duration = "--"
                if s.get('started_at') and s.get('completed_at'):
                    try:
                        ss = datetime.fromisoformat(s['started_at'].replace('Z', '+00:00'))
                        se = datetime.fromisoformat(s['completed_at'].replace('Z', '+00:00'))
                        step_duration = _format_duration((se - ss).total_seconds())
                    except Exception:
                        pass
                steps.append({
                    'name': s.get('name', ''),
                    'status': s.get('status', ''),
                    'conclusion': s.get('conclusion'),
                    'number': s.get('number', 0),
                    'duration': step_duration,
                })

            runner = j.get('runner_name', '')
            if not runner and j.get('labels'):
                runner = j['labels'][0]

            jobs.append({
                'id': j['id'],
                'name': j.get('name', ''),
                'status': j.get('status', ''),
                'conclusion': j.get('conclusion'),
                'duration': duration,
                'runner_name': runner or 'unknown',
                'steps': steps,
                'html_url': j.get('html_url', ''),
            })

        cache_set(cache_key, jobs)
        return jsonify(jobs)

    except Exception as e:
        print(f"[list_run_jobs] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/jobs/<int:job_id>/logs')
@login_required
def get_job_logs(owner, repo, job_id):
    """Fetch logs for a specific job. Truncates to last 2000 lines."""
    try:
        url = f'{GITHUB_API}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs'
        r = http.get(url, headers=_headers(), timeout=30, allow_redirects=True)
        if r.status_code == 200:
            lines = r.text.split('\n')
            total = len(lines)
            truncated = total > 2000
            if truncated:
                lines = lines[-2000:]
            return jsonify({
                'content': '\n'.join(lines),
                'truncated': truncated,
                'total_lines': total,
            })
        else:
            return jsonify({'error': f'GitHub returned {r.status_code}'}), r.status_code

    except Exception as e:
        print(f"[get_job_logs] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/workflows/<int:workflow_id>/trends')
@login_required
def get_workflow_trends(owner, repo, workflow_id):
    """Fetch duration trends for the last 20 completed runs of a workflow."""
    try:
        cache_key = f'trends:{owner}/{repo}/{workflow_id}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        data = _github_get(
            f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs',
            {'per_page': 20, 'status': 'completed'}
        )

        points = []
        for run in data.get('workflow_runs', []):
            duration_sec = 0
            if run.get('created_at') and run.get('updated_at'):
                try:
                    start = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
                    duration_sec = int((end - start).total_seconds())
                except Exception:
                    pass
            points.append({
                'run_number': run.get('run_number', 0),
                'conclusion': run.get('conclusion', ''),
                'duration_sec': duration_sec,
                'created_at': run.get('created_at', ''),
            })

        result = {'workflow_id': workflow_id, 'points': list(reversed(points))}
        cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        print(f"[get_workflow_trends] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/analytics')
@login_required
def get_repo_analytics(owner, repo):
    """Compute failure analytics for a repo's workflows."""
    try:
        cache_key = f'analytics:{owner}/{repo}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        wf_data = _github_get(f'/repos/{owner}/{repo}/actions/workflows')
        active = [w for w in wf_data.get('workflows', []) if w.get('state') == 'active']

        analytics = []
        for wf in active[:10]:
            try:
                runs_data = _github_get(
                    f'/repos/{owner}/{repo}/actions/workflows/{wf["id"]}/runs',
                    {'per_page': 30}
                )
                runs = runs_data.get('workflow_runs', [])
                total = len(runs)
                successes = sum(1 for r in runs if r.get('conclusion') == 'success')
                failures = sum(1 for r in runs if r.get('conclusion') == 'failure')

                # Flaky detection: count conclusion alternations
                conclusions = [r.get('conclusion') for r in runs if r.get('conclusion')]
                flaky_count = sum(1 for i in range(1, len(conclusions))
                                  if conclusions[i] != conclusions[i - 1])

                # MTTR: avg time from failure to next success
                mttr_values = []
                for i, r in enumerate(runs):
                    if r.get('conclusion') == 'failure':
                        for j in range(i - 1, -1, -1):
                            if runs[j].get('conclusion') == 'success':
                                try:
                                    ft = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
                                    st = datetime.fromisoformat(runs[j]['created_at'].replace('Z', '+00:00'))
                                    mttr_values.append(abs((st - ft).total_seconds()))
                                except Exception:
                                    pass
                                break

                avg_mttr = int(sum(mttr_values) / len(mttr_values)) if mttr_values else 0

                analytics.append({
                    'workflow_id': wf['id'],
                    'name': wf['name'],
                    'total_runs': total,
                    'successes': successes,
                    'failures': failures,
                    'success_rate': round(successes / total * 100, 1) if total else 0,
                    'is_flaky': flaky_count >= 3,
                    'flaky_score': flaky_count,
                    'mttr_seconds': avg_mttr,
                    'mttr_display': _format_duration(avg_mttr) if avg_mttr else '--',
                })
            except Exception as e:
                print(f"[analytics] Error for workflow {wf['name']}: {e}")

        analytics.sort(key=lambda a: a['failures'], reverse=True)
        cache_set(cache_key, analytics)
        return jsonify(analytics)

    except Exception as e:
        print(f"[get_repo_analytics] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/activity')
@login_required
def get_activity():
    """Get recent activity across all repos (up to 50 most recent runs)."""
    try:
        cache_key = f'activity:{get_token()[:8] if get_token() else "anon"}'
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        repos_resp = list_repos()
        repos = repos_resp.get_json()
        if isinstance(repos, dict) and 'error' in repos:
            return jsonify(repos), 500

        repo_list = repos if isinstance(repos, list) else []
        max_scan = min(len(repo_list), 15)
        all_runs = []

        for rp in repo_list[:max_scan]:
            o, n = rp['full_name'].split('/')
            try:
                data = _github_get(f'/repos/{o}/{n}/actions/runs', {'per_page': 10})
                for run in data.get('workflow_runs', []):
                    duration = "--"
                    if run.get('status') == 'completed' and run.get('created_at') and run.get('updated_at'):
                        try:
                            start = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
                            end = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
                            duration = _format_duration((end - start).total_seconds())
                        except Exception:
                            pass
                    elif run.get('status') == 'in_progress':
                        duration = 'running...'

                    all_runs.append({
                        'id': run['id'],
                        'repo': rp['full_name'],
                        'repo_name': rp['name'],
                        'name': run['name'],
                        'status': run['status'],
                        'conclusion': run.get('conclusion'),
                        'branch': run.get('head_branch', ''),
                        'triggered_by': (run.get('actor') or {}).get('login', '--'),
                        'created_at': run.get('created_at', ''),
                        'duration': duration,
                        'url': run.get('html_url', ''),
                    })
            except Exception as e:
                print(f"[activity] Error for {rp['full_name']}: {e}")

        all_runs.sort(key=lambda r: r.get('created_at', ''), reverse=True)
        result = all_runs[:50]
        cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        print(f"[activity] Error: {e}")
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('PIPELINE_HUB_PORT', '9090'))
    app.run(host='0.0.0.0', port=port, debug=False)
