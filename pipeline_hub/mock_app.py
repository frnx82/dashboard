"""
Pipeline Hub — Mock App (Local Development)
=============================================
Standalone mock version with realistic fake data. No GitHub token needed.
Run this to preview the UI locally:

    python mock_app.py

Then open http://localhost:9090
"""

from flask import Flask, render_template, jsonify, request
import random, time, threading
from datetime import datetime, timedelta

app = Flask(__name__)

print("[Pipeline Hub] Running in MOCK mode — no GitHub token needed")

# ──────────────────────────────────────────────────────────────────────────────
# Mock Data
# ──────────────────────────────────────────────────────────────────────────────

REPOS = [
    {"name": "frontend-app", "full_name": "my-org/frontend-app", "language": "TypeScript", "default_branch": "main", "visibility": "private"},
    {"name": "backend-api", "full_name": "my-org/backend-api", "language": "Python", "default_branch": "main", "visibility": "private"},
    {"name": "billing-service", "full_name": "my-org/billing-service", "language": "Go", "default_branch": "main", "visibility": "private"},
    {"name": "infra-terraform", "full_name": "my-org/infra-terraform", "language": "HCL", "default_branch": "main", "visibility": "private"},
    {"name": "mobile-app", "full_name": "my-org/mobile-app", "language": "Kotlin", "default_branch": "develop", "visibility": "private"},
    {"name": "data-pipeline", "full_name": "my-org/data-pipeline", "language": "Python", "default_branch": "main", "visibility": "private"},
    {"name": "auth-service", "full_name": "my-org/auth-service", "language": "Java", "default_branch": "main", "visibility": "private"},
    {"name": "notification-svc", "full_name": "my-org/notification-svc", "language": "Node.js", "default_branch": "main", "visibility": "private"},
]

WORKFLOWS = {
    "my-org/frontend-app": [
        {"id": 1, "name": "Build & Test", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "3 min ago", "duration": "2m 45s",
         "last_run_by": "john.doe", "branch": "main", "dispatch_inputs": []},
        {"id": 2, "name": "Deploy to Staging", "file": "deploy-staging.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "25 min ago", "duration": "4m 12s",
         "last_run_by": "jane.smith", "branch": "main",
         "dispatch_inputs": [{"name": "environment", "type": "choice", "options": ["staging", "dev"], "default": "staging"}]},
        {"id": 3, "name": "Deploy to Production", "file": "deploy-prod.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "2 days ago", "duration": "6m 30s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [
             {"name": "environment", "type": "choice", "options": ["production"], "default": "production"},
             {"name": "confirm", "type": "boolean", "default": "false"}
         ]},
        {"id": 4, "name": "Lighthouse Audit", "file": "lighthouse.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 day ago", "duration": "3m 15s",
         "last_run_by": "ci-bot", "branch": "main", "dispatch_inputs": []},
        {"id": 5, "name": "E2E Tests", "file": "e2e.yml", "state": "active",
         "last_conclusion": "failure", "last_run_ago": "4 hours ago", "duration": "8m 02s",
         "last_run_by": "john.doe", "branch": "feature/auth-v2", "dispatch_inputs": []},
    ],
    "my-org/backend-api": [
        {"id": 10, "name": "Build & Test", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "8 min ago", "duration": "3m 24s",
         "last_run_by": "rajesh.e", "branch": "main", "dispatch_inputs": []},
        {"id": 11, "name": "Deploy to Staging", "file": "deploy-staging.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 hour ago", "duration": "5m 12s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "tag", "type": "string", "default": "latest"}]},
        {"id": 12, "name": "Deploy to Production", "file": "deploy-prod.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "3 days ago", "duration": "7m 45s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [
             {"name": "tag", "type": "string", "default": ""},
             {"name": "environment", "type": "choice", "options": ["production", "dr-site"], "default": "production"}
         ]},
        {"id": 13, "name": "Security Scan", "file": "security.yml", "state": "active",
         "last_conclusion": "failure", "last_run_ago": "12 hours ago", "duration": "2m 08s",
         "last_run_by": "ci-bot", "branch": "main", "dispatch_inputs": []},
        {"id": 14, "name": "Release", "file": "release.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "5 days ago", "duration": "4m 30s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "version", "type": "string", "default": ""}]},
    ],
    "my-org/billing-service": [
        {"id": 20, "name": "Build & Unit Tests", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "15 min ago", "duration": "1m 58s",
         "last_run_by": "mike.lee", "branch": "main", "dispatch_inputs": []},
        {"id": 21, "name": "Integration Tests", "file": "integration.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "2 hours ago", "duration": "6m 45s",
         "last_run_by": "mike.lee", "branch": "main", "dispatch_inputs": []},
        {"id": 22, "name": "Deploy", "file": "deploy.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 day ago", "duration": "5m 22s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "target", "type": "choice", "options": ["staging", "production"], "default": "staging"}]},
        {"id": 23, "name": "PCI Compliance Scan", "file": "pci-scan.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 week ago", "duration": "12m 10s",
         "last_run_by": "security-bot", "branch": "main", "dispatch_inputs": []},
    ],
    "my-org/infra-terraform": [
        {"id": 30, "name": "Terraform Plan", "file": "plan.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "30 min ago", "duration": "2m 15s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "workspace", "type": "choice", "options": ["dev", "staging", "production"], "default": "dev"}]},
        {"id": 31, "name": "Terraform Apply", "file": "apply.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "2 days ago", "duration": "8m 30s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [
             {"name": "workspace", "type": "choice", "options": ["dev", "staging", "production"], "default": "dev"},
             {"name": "auto_approve", "type": "boolean", "default": "false"}
         ]},
        {"id": 32, "name": "Drift Detection", "file": "drift.yml", "state": "active",
         "last_conclusion": "failure", "last_run_ago": "6 hours ago", "duration": "3m 45s",
         "last_run_by": "ci-bot", "branch": "main", "dispatch_inputs": []},
        {"id": 33, "name": "Cost Estimation", "file": "infracost.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 day ago", "duration": "1m 42s",
         "last_run_by": "ci-bot", "branch": "main", "dispatch_inputs": []},
    ],
    "my-org/mobile-app": [
        {"id": 40, "name": "Android Build", "file": "android-ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "45 min ago", "duration": "11m 20s",
         "last_run_by": "sarah.k", "branch": "develop", "dispatch_inputs": []},
        {"id": 41, "name": "iOS Build", "file": "ios-ci.yml", "state": "active",
         "last_conclusion": "failure", "last_run_ago": "1 hour ago", "duration": "14m 05s",
         "last_run_by": "sarah.k", "branch": "develop", "dispatch_inputs": []},
        {"id": 42, "name": "Publish to TestFlight", "file": "testflight.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "3 days ago", "duration": "18m 30s",
         "last_run_by": "sarah.k", "branch": "release/2.5",
         "dispatch_inputs": [{"name": "build_number", "type": "string", "default": ""}]},
        {"id": 43, "name": "Play Store Release", "file": "play-store.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "5 days ago", "duration": "9m 15s",
         "last_run_by": "rajesh.e", "branch": "release/2.5",
         "dispatch_inputs": [{"name": "track", "type": "choice", "options": ["internal", "alpha", "beta", "production"], "default": "internal"}]},
    ],
    "my-org/data-pipeline": [
        {"id": 50, "name": "Build & Test", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "20 min ago", "duration": "4m 12s",
         "last_run_by": "data-team", "branch": "main", "dispatch_inputs": []},
        {"id": 51, "name": "Deploy DAGs", "file": "deploy-dags.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "3 hours ago", "duration": "2m 50s",
         "last_run_by": "data-team", "branch": "main",
         "dispatch_inputs": [{"name": "environment", "type": "choice", "options": ["dev", "production"], "default": "dev"}]},
        {"id": 52, "name": "Data Quality Check", "file": "dq-check.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "6 hours ago", "duration": "15m 40s",
         "last_run_by": "ci-bot", "branch": "main", "dispatch_inputs": []},
    ],
    "my-org/auth-service": [
        {"id": 60, "name": "Build & Test", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 hour ago", "duration": "5m 30s",
         "last_run_by": "sec-team", "branch": "main", "dispatch_inputs": []},
        {"id": 61, "name": "Deploy", "file": "deploy.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "2 days ago", "duration": "4m 10s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "environment", "type": "choice", "options": ["staging", "production"], "default": "staging"}]},
        {"id": 62, "name": "OWASP ZAP Scan", "file": "zap-scan.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 day ago", "duration": "7m 20s",
         "last_run_by": "security-bot", "branch": "main", "dispatch_inputs": []},
    ],
    "my-org/notification-svc": [
        {"id": 70, "name": "Build & Test", "file": "ci.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "35 min ago", "duration": "2m 10s",
         "last_run_by": "dev-team", "branch": "main", "dispatch_inputs": []},
        {"id": 71, "name": "Deploy", "file": "deploy.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "4 hours ago", "duration": "3m 55s",
         "last_run_by": "rajesh.e", "branch": "main",
         "dispatch_inputs": [{"name": "environment", "type": "choice", "options": ["staging", "production"], "default": "staging"}]},
        {"id": 72, "name": "Load Test", "file": "load-test.yml", "state": "active",
         "last_conclusion": "success", "last_run_ago": "1 week ago", "duration": "22m 15s",
         "last_run_by": "perf-team", "branch": "main",
         "dispatch_inputs": [{"name": "users", "type": "string", "default": "100"},
                             {"name": "duration", "type": "string", "default": "5m"}]},
    ],
}

TRIGGERED_RUNS = []
RUN_COUNTER = 400


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config')
def get_config():
    return jsonify({'mode': 'mock', 'org': 'my-org', 'repos_filter': []})


@app.route('/api/repos')
def list_repos():
    return jsonify(REPOS)


@app.route('/api/repos/<owner>/<repo>/workflows')
def list_workflows(owner, repo):
    return jsonify(WORKFLOWS.get(f"{owner}/{repo}", []))


@app.route('/api/repos/<owner>/<repo>/runs')
def list_runs(owner, repo):
    full_name = f"{owner}/{repo}"
    workflows = WORKFLOWS.get(full_name, [])
    runs = []
    now = datetime.now()
    for i, w in enumerate(workflows):
        runs.append({
            'id': 300 + i, 'run_number': 300 + i,
            'name': w['name'], 'workflow_file': w['file'],
            'status': 'completed', 'conclusion': w['last_conclusion'],
            'branch': w.get('branch', 'main'),
            'triggered_by': w.get('last_run_by', 'unknown'),
            'created_at': (now - timedelta(minutes=random.randint(5, 1440))).isoformat(),
            'duration': w.get('duration', '3m 00s'),
            'url': f'https://github.com/{full_name}/actions/runs/{300+i}',
        })
    for tr in TRIGGERED_RUNS:
        if tr.get('repo') == full_name:
            runs.insert(0, tr)
    runs.sort(key=lambda r: r['created_at'], reverse=True)
    return jsonify(runs[:15])


@app.route('/api/repos/<owner>/<repo>/workflows/<int:workflow_id>/run', methods=['POST'])
def trigger_workflow(owner, repo, workflow_id):
    global RUN_COUNTER
    full_name = f"{owner}/{repo}"
    workflows = WORKFLOWS.get(full_name, [])
    wf = next((w for w in workflows if w['id'] == workflow_id), None)
    if not wf:
        return jsonify({'error': 'Workflow not found'}), 404

    data = request.json or {}
    branch = data.get('branch', 'main')

    RUN_COUNTER += 1
    run = {
        'id': RUN_COUNTER, 'run_number': RUN_COUNTER,
        'name': wf['name'], 'workflow_file': wf['file'],
        'status': 'in_progress', 'conclusion': None,
        'branch': branch, 'triggered_by': 'you',
        'created_at': datetime.now().isoformat(),
        'duration': 'running...', 'repo': full_name,
        'url': f'https://github.com/{full_name}/actions/runs/{RUN_COUNTER}',
    }
    TRIGGERED_RUNS.insert(0, run)

    def _complete():
        time.sleep(random.randint(3, 8))
        run['status'] = 'completed'
        run['conclusion'] = random.choice(['success', 'success', 'success', 'failure'])
        run['duration'] = f'{random.randint(1,12)}m {random.randint(10,59)}s'
    threading.Thread(target=_complete, daemon=True).start()

    return jsonify({'status': 'triggered', 'run_id': RUN_COUNTER,
                    'message': f'✅ {wf["name"]} triggered on {branch}'})


@app.route('/api/stats')
def global_stats():
    total = sum(len(wfs) for wfs in WORKFLOWS.values())
    passing = sum(1 for wfs in WORKFLOWS.values() for w in wfs if w['last_conclusion'] == 'success')
    failing = sum(1 for wfs in WORKFLOWS.values() for w in wfs if w['last_conclusion'] == 'failure')
    return jsonify({
        'total_repos': len(REPOS),
        'total_workflows': total,
        'passing': passing,
        'failing': failing,
        'success_rate': round(passing / total * 100, 1) if total else 0,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9090, debug=True)
