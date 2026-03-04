"""
Pipeline Hub — GitHub Workflow Dashboard (Production)
=====================================================
A Flask app that aggregates GitHub Actions workflows from multiple repos,
lets you trigger builds, and monitor run status — all from one place.

Environment Variables:
    GITHUB_TOKEN       — GitHub Personal Access Token (required)
    GITHUB_ORG         — GitHub org/user to list repos from (optional)
    GITHUB_REPOS       — Comma-separated list of specific repos (optional, overrides org listing)
    PIPELINE_HUB_PORT  — Port to run on (default: 9090)

For local development without a GitHub token, use mock_app.py instead.
"""

from flask import Flask, render_template, jsonify, request
import os, json, base64
from datetime import datetime

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
GITHUB_ORG = os.getenv('GITHUB_ORG', '')
GITHUB_REPOS = [r.strip() for r in os.getenv('GITHUB_REPOS', '').split(',') if r.strip()]
GITHUB_API = 'https://api.github.com'

if not GITHUB_TOKEN:
    print("⚠️  [Pipeline Hub] No GITHUB_TOKEN set. API calls will fail.")
    print("    For local testing, use: python mock_app.py")
else:
    print(f"[Pipeline Hub] LIVE mode — org: {GITHUB_ORG or 'auto'}, repos filter: {len(GITHUB_REPOS) or 'all'}")


# ──────────────────────────────────────────────────────────────────────────────
# GitHub API Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _headers():
    return {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'PipelineHub/1.0'
    }


def _github_get(path, params=None):
    """GET request to GitHub API with error handling."""
    import requests
    url = f'{GITHUB_API}{path}'
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"[GitHub API] HTTP {e.response.status_code} for {path}: {e.response.text[:200]}")
        raise
    except Exception as e:
        print(f"[GitHub API] Error for {path}: {e}")
        raise


def _github_post(path, data=None):
    """POST request to GitHub API."""
    import requests
    url = f'{GITHUB_API}{path}'
    try:
        r = requests.post(url, headers=_headers(), json=data, timeout=15)
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
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config')
def get_config():
    return jsonify({
        'mode': 'live',
        'org': GITHUB_ORG or 'auto',
        'repos_filter': GITHUB_REPOS,
        'token_set': bool(GITHUB_TOKEN),
    })


@app.route('/api/repos')
def list_repos():
    try:
        repos = []

        if GITHUB_REPOS:
            # Mode 1: Specific repos configured via GITHUB_REPOS env var
            for repo_name in GITHUB_REPOS:
                try:
                    path = f'/repos/{repo_name}' if '/' in repo_name else f'/repos/{GITHUB_ORG}/{repo_name}'
                    data = _github_get(path)
                    repos.append({
                        'name': data['name'],
                        'full_name': data['full_name'],
                        'language': data.get('language') or 'Unknown',
                        'default_branch': data.get('default_branch', 'main'),
                        'visibility': data.get('visibility', 'private'),
                    })
                except Exception as e:
                    print(f"[list_repos] Skipping {repo_name}: {e}")

        elif GITHUB_ORG:
            # Mode 2: All repos from an org
            page = 1
            while True:
                data = _github_get(f'/orgs/{GITHUB_ORG}/repos', {
                    'per_page': 100, 'page': page, 'sort': 'updated', 'type': 'all'
                })
                if not data:
                    break
                for r in data:
                    if r.get('archived'):
                        continue
                    repos.append({
                        'name': r['name'],
                        'full_name': r['full_name'],
                        'language': r.get('language') or 'Unknown',
                        'default_branch': r.get('default_branch', 'main'),
                        'visibility': r.get('visibility', 'private'),
                    })
                if len(data) < 100:
                    break
                page += 1

        else:
            # Mode 3: All repos accessible by the token owner
            data = _github_get('/user/repos', {
                'per_page': 100, 'sort': 'updated',
                'affiliation': 'owner,collaborator,organization_member'
            })
            for r in data:
                if r.get('archived'):
                    continue
                repos.append({
                    'name': r['name'],
                    'full_name': r['full_name'],
                    'language': r.get('language') or 'Unknown',
                    'default_branch': r.get('default_branch', 'main'),
                    'visibility': r.get('visibility', 'private'),
                })

        repos.sort(key=lambda r: r['name'])
        return jsonify(repos)

    except Exception as e:
        print(f"[list_repos] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/workflows')
def list_workflows(owner, repo):
    try:
        data = _github_get(f'/repos/{owner}/{repo}/actions/workflows')
        workflows = []

        for w in data.get('workflows', []):
            if w.get('state') != 'active':
                continue

            # Get last run for this workflow
            last_conclusion = None
            last_run_ago = "Never"
            last_run_by = "--"
            duration = "--"
            branch = ""

            try:
                runs_data = _github_get(
                    f'/repos/{owner}/{repo}/actions/workflows/{w["id"]}/runs',
                    {'per_page': 1}
                )
                runs = runs_data.get('workflow_runs', [])
                if runs:
                    run = runs[0]
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
            except Exception as e:
                print(f"[list_workflows] Error getting runs for {w['name']}: {e}")

            # Parse workflow_dispatch inputs from the YAML file
            dispatch_inputs = []
            try:
                file_data = _github_get(f'/repos/{owner}/{repo}/contents/{w["path"]}')
                if file_data.get('content'):
                    dispatch_inputs = _parse_workflow_inputs(file_data['content'])
            except Exception:
                pass

            workflows.append({
                'id': w['id'],
                'name': w['name'],
                'file': w['path'].split('/')[-1],
                'state': w['state'],
                'last_conclusion': last_conclusion,
                'last_run_ago': last_run_ago,
                'duration': duration,
                'last_run_by': last_run_by,
                'branch': branch,
                'dispatch_inputs': dispatch_inputs,
            })

        return jsonify(workflows)

    except Exception as e:
        print(f"[list_workflows] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/repos/<owner>/<repo>/runs')
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
            return jsonify({'status': 'triggered', 'message': f'✅ Workflow triggered on {branch}'})
        elif response.status_code == 422:
            error_detail = ""
            try:
                error_detail = response.json().get('message', '')
            except Exception:
                error_detail = response.text[:200]
            return jsonify({'error': f'Cannot trigger: {error_detail}. Ensure workflow has workflow_dispatch trigger and branch exists.'}), 422
        elif response.status_code == 403:
            return jsonify({'error': 'Permission denied. Token needs workflow scope.'}), 403
        elif response.status_code == 404:
            return jsonify({'error': 'Workflow or repo not found. Check permissions.'}), 404
        else:
            return jsonify({'error': f'GitHub returned {response.status_code}: {response.text[:200]}'}), response.status_code

    except Exception as e:
        print(f"[trigger_workflow] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats')
def global_stats():
    try:
        repos_resp = list_repos()
        repos = repos_resp.get_json()
        if isinstance(repos, dict) and 'error' in repos:
            return jsonify(repos), 500

        total_workflows = 0
        passing = 0
        failing = 0

        for repo in repos[:20]:  # Limit to avoid GitHub rate-limiting
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
            except Exception:
                pass

        return jsonify({
            'total_repos': len(repos),
            'total_workflows': total_workflows,
            'passing': passing,
            'failing': failing,
            'success_rate': round(passing / total_workflows * 100, 1) if total_workflows else 0,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('PIPELINE_HUB_PORT', '9090'))
    app.run(host='0.0.0.0', port=port, debug=False)
