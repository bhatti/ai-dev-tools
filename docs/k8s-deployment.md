# Deployment Guide

Complete step-by-step instructions. Two tracks:
- **Local (laptop)** — all-in-one: formicary + embedded ant + Slack router in Docker Desktop k8s
- **EC2 + laptops** — leader on EC2, each developer runs a personal ant worker on their laptop

Run `make test` in both repos before doing anything else.

---

## Validate first (any machine)

```bash
cd ~/workplace/formicary
make test          # Go build + all tests including WAL pragma + TLS tests

cd ~/workplace/ai-dev-tools
make test          # 306 Python tests
```

Both must be green before you deploy anything.

---

## Track A — Local single-machine (Docker Desktop k8s)

Use this to validate everything before touching EC2.

### A1. Build images locally

```bash
cd ~/workplace/formicary
make docker-build
# → plexobject/formicary:latest

cd ~/workplace/ai-dev-tools
make build
# → plexobject/ai-dev-tools:latest
```

No push needed for local use — Docker Desktop can see locally-built images.

### A2. Deploy formicary (all-in-one: leader + embedded ant)

```bash
cd ~/workplace/formicary

# One-time: create auth secret
kubectl create secret generic formicary-auth \
  --from-literal=jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=google-client-id="" \
  --from-literal=google-client-secret=""

kubectl apply -f k8s/formicary-all-in-one.yaml
kubectl rollout status deployment/formicary --timeout=120s

# Expose ports in a separate terminal (keep running)
kubectl port-forward svc/formicary 7777:7777 19000:19000
```

Open http://localhost:7777 — register an account. Go to **Profile → API Tokens → New Token**. Copy it.

```bash
export FORMICARY_TOKEN="<token from UI>"
export FORMICARY_URL="http://localhost:7777"
```

### A3. Create credentials secret + upload workflows

Pick GitHub OR Jira depending on your tracker.

**GitHub:**
```bash
cd ~/workplace/formicary/docs/examples

export GH_TOKEN="ghp_..."
export SSH_PRIVATE_KEY="$(cat ~/.ssh/id_rsa)"
export SLACK_BOT_TOKEN="xoxb-..."       # set after Slack app setup (Step A5)
export SLACK_APP_TOKEN="xapp-..."       # set after Slack app setup (Step A5)
export BEDROCK_URL="http://ai/bedrock"  # or leave blank if using ANTHROPIC_API_KEY

./deploy-ai-workflows.sh \
  --server "$FORMICARY_URL" \
  --create-k8s-secret \
  --set-configs \
  --gh-org YOUR_ORG \
  --gh-repo YOUR_REPO \
  --bedrock \
  --ant-user-tag "${USER}"
```

**Jira + Bitbucket:**
```bash
cd ~/workplace/formicary/docs/examples

export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="..."
export BITBUCKET_TOKEN="..."
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."

./deploy-ai-jira-workflows.sh \
  --server "$FORMICARY_URL" \
  --create-k8s-secret \
  --set-configs \
  --jira-project MYPROJ \
  --bb-workspace YOUR_WORKSPACE \
  --bb-repo YOUR_REPO \
  --bedrock \
  --ant-user-tag "${USER}"
```

### A4. Deploy Slack router

```bash
cd ~/workplace/formicary/docs/examples

# Requires SLACK_BOT_TOKEN, SLACK_APP_TOKEN, FORMICARY_TOKEN already exported
./deploy-ai-slack-router.sh \
  --server "$FORMICARY_URL" \
  --create-k8s-secret \
  --set-configs \
  --slack-channel "#your-channel" \
  --bot-name "@your-bot-name" \
  --default-tracker jira \
  --bedrock

kubectl logs -f deployment/ai-slack-router
# Expected: "[router] connected to Slack via Socket Mode"
```

### A5. Slack app setup (one-time, before A3/A4)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Socket Mode** → Enable → **App-Level Tokens** → Generate → scope: `connections:write` → copy `xapp-...` → `export SLACK_APP_TOKEN=xapp-...`
3. **OAuth & Permissions** → Bot Token Scopes → add: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `groups:history`, `groups:read`, `users:read`
4. **Install to Workspace** → copy `xoxb-...` → `export SLACK_BOT_TOKEN=xoxb-...`
5. **Event Subscriptions** → Enable → Subscribe to bot events: `app_mention`, `message.channels`, `message.groups` → Save
6. **Interactivity & Shortcuts** → Enable (for Block Kit buttons in reviews)
7. In Slack: `/invite @your-bot-name` in your channel

Find your bot's exact name:
```bash
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" | python3 -m json.tool | grep '"user"'
```

### A6. Smoke test

```bash
# In Slack:
@your-bot-name doctor
# → "Started ai-connectivity-check (job abc123)" then results in thread

@your-bot-name standup
# → standup brief with bullet rows, ALL-CAPS headers

@your-bot-name jira-query flaky tests
# → matching issues listed

@your-bot-name qjira anything
# → "I don't have a workflow for that. Try @your-bot-name help"  (qjira removed)
```

### A7. Change a single org config

```bash
cd ~/workplace/ai-dev-tools

# Set the branch that implement workflows fork from
python -m scripts.slack.deploy_workflows \
  --server "$FORMICARY_URL" \
  --set-config BASE_BRANCH stage

# Change AI model without rebuilding
python -m scripts.slack.deploy_workflows \
  --server "$FORMICARY_URL" \
  --set-config AnthropicSonnetModel us.anthropic.claude-sonnet-5
```

---

## Track B — EC2 leader + per-developer ant workers

### B1. Prerequisites on EC2

- EC2 instance with k3s running
- Port 7777 open in security group (formicary API/UI)
- Port 19000 open (artifacts/S3)
- Port 443 open (HTTPS via iptables DNAT → pod:7777) — needed for Google OAuth redirect URIs
- Port 6443 (k3s API) does **not** need to be open — the deploy scripts SSH into EC2 to run kubectl

**HTTPS + OAuth setup (production):**

Google OAuth requires a real domain — bare IP redirect URIs are blocked. Use nip.io:
- Self-signed TLS cert with SAN for `<ip>.nip.io`
- Callback URL registered in Google Cloud Console: `https://<ip>.nip.io/auth/google/callback`
- iptables DNAT: `443 → pod:7777` with `! -s 10.42.0.0/16` to exclude pod-to-pod traffic

Set in `~/.zshrc` on your laptop:
```bash
export FORMICARY_URL="https://<EC2_IP>.nip.io"
```
All deploy scripts auto-add `-k` for curl when the URL is HTTPS (self-signed cert).

### B2. Clone repos on EC2

SSH into EC2:

```bash
ssh ec2-user@<EC2_IP>

git clone https://github.com/bhatti/formicary.git ~/formicary
git clone https://github.com/bhatti/ai-dev-tools.git ~/ai-dev-tools
```

> **Before check-in:** if you haven't pushed your changes yet, copy them to EC2
> manually or use `rsync`:
> ```bash
> rsync -av --exclude='.git' --exclude='target' --exclude='vendor' \
>   ~/workplace/formicary/ ec2-user@<EC2_IP>:~/formicary/
> rsync -av --exclude='.git' --exclude='.venv' \
>   ~/workplace/ai-dev-tools/ ec2-user@<EC2_IP>:~/ai-dev-tools/
> ```

### B3. Build and push images (from your laptop)

```bash
cd ~/workplace/formicary
make docker-build
docker push plexobject/formicary:latest

cd ~/workplace/ai-dev-tools
make build
docker push plexobject/ai-dev-tools:latest
```

Or build directly on EC2 if it has Docker:
```bash
ssh ec2-user@<EC2_IP> "cd ~/formicary && make docker-build"
ssh ec2-user@<EC2_IP> "cd ~/ai-dev-tools && make build"
```

### B4. Deploy formicary leader on EC2

Run on EC2:

```bash
cd ~/formicary

# One-time auth secret
kubectl create secret generic formicary-auth \
  --from-literal=jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=google-client-id="" \
  --from-literal=google-client-secret=""

kubectl apply -f k8s/formicary-leader.yaml
kubectl rollout status deployment/formicary --timeout=120s
kubectl get pods   # formicary-xxx Running
```

Expose ports (run in background, keep alive):
```bash
nohup kubectl port-forward svc/formicary 7777:7777 19000:19000 --address=0.0.0.0 &
```

> `--address=0.0.0.0` makes the port reachable from outside EC2 (via security group).
> For production use an actual LoadBalancer or NodePort Service instead.

Open `http://EC2_IP:7777` from your browser. Register, generate API token.

```bash
export FORMICARY_TOKEN="<token>"
export FORMICARY_URL="http://EC2_IP:7777"    # or https:// if TLS enabled
```

### B5. Enable native HTTPS on EC2 (optional but recommended)

Formicary has native TLS — no nginx proxy needed. The `TLSConfig` is defined in
`proto/formicary/v1/domain/common.proto` and wired into the server listener.

**Self-signed cert for testing:**
```bash
cd ~/formicary
make gen-tls-certs
# → certs/tls.crt, certs/tls.key
```

**Let's Encrypt cert (requires domain pointing at EC2):**
```bash
sudo certbot certonly --standalone -d formicary.example.com \
  --non-interactive --agree-tos -m you@example.com
# → /etc/letsencrypt/live/formicary.example.com/fullchain.pem + privkey.pem
```

**Create Kubernetes TLS secret:**
```bash
# Self-signed:
kubectl create secret tls formicary-tls \
  --cert=certs/tls.crt --key=certs/tls.key

# Let's Encrypt:
kubectl create secret tls formicary-tls \
  --cert=/etc/letsencrypt/live/formicary.example.com/fullchain.pem \
  --key=/etc/letsencrypt/live/formicary.example.com/privkey.pem
```

**Enable TLS in `k8s/formicary-leader.yaml`** — uncomment three blocks:

```yaml
# 1. In ConfigMap formicary-queen.yaml:
common:
    http_port: 7777
    tls:
        enabled: true
        cert_file: /certs/tls.crt
        key_file:  /certs/tls.key

# 2. In Deployment volumeMounts:
- name: tls-certs
  mountPath: /certs
  readOnly: true

# 3. In Deployment volumes:
- name: tls-certs
  secret:
    secretName: formicary-tls
```

Then apply:
```bash
kubectl apply -f k8s/formicary-leader.yaml
kubectl rollout restart deployment/formicary

export FORMICARY_URL="https://formicary.example.com"
# For self-signed: add --insecure to curl during testing
```

### B6. Upload workflows + set org configs (from your laptop)

`FORMICARY_URL` should already be in `~/.zshrc` (e.g. `https://<ec2-ip>.nip.io`).
The deploy scripts automatically add `-k` to curl calls when the URL is HTTPS,
so the self-signed cert doesn't cause failures.

```bash
source ~/.zshrc    # loads FORMICARY_URL, FORMICARY_TOKEN, JIRA_*, BITBUCKET_*, etc.

cd ~/workplace/formicary/docs/examples

# Jira + Bitbucket workflows (credentials auto-read from ~/.config/acli/config.json):
./deploy-ai-jira-workflows.sh \
  --create-k8s-secret \
  --set-configs \
  --jira-project MYPROJ \
  --bb-workspace YOUR_WORKSPACE \
  --bb-repo YOUR_REPO \
  --bedrock \
  --ant-user-tag "${USER}"

# GitHub workflows (if applicable):
./deploy-ai-workflows.sh \
  --create-k8s-secret \
  --set-configs \
  --gh-org YOUR_ORG \
  --gh-repo YOUR_REPO \
  --bedrock \
  --ant-user-tag "${USER}"
```

### B7. Deploy Slack router on EC2

The router runs on EC2 inside the same cluster as the leader. It connects to
Formicary via the internal k8s service name `http://formicary:7777` (already
set in `ai-slack-router-k8s.yaml`).

**EC2 k3s note:** port 6443 (k3s API) is not open in the security group.
The deploy script handles this automatically with `--ec2-ip` — every `kubectl`
call is transparently remapped to run over SSH using the EC2 key.

Run from your laptop:

```bash
cd ~/workplace/formicary/docs/examples

# Credentials already in ~/.zshrc:
#   FORMICARY_TOKEN, FORMICARY_URL=https://10.8.x.y.nip.io
#   SLACK_BOT_TOKEN, SLACK_APP_TOKEN
source ~/.zshrc

./deploy-ai-slack-router.sh \
  --ec2-ip <EC2_IP> \
  --set-configs \
  --bot-name "@your-bot-name" \
  --default-tracker jira \
  --bedrock
```

Options:
- `--ec2-ip <ip>` — SSH kubectl to EC2 instead of using local kubeconfig
- `--ec2-key <path>` — SSH key (default: `~/ec2-key.pem`; set `EC2_KEY` env var or pass `--ec2-key`)
- `--ec2-user <user>` — SSH user (default: `ec2-user`)
- `--slack-channel` — **optional**; router auto-derives the reply channel from each Slack event
- `FORMICARY_URL` auto-set to `https://<ip>.nip.io` when not explicitly set; all curl calls use `-k` for the self-signed cert

**Verify the router connected:**
```bash
./deploy-ai-slack-router.sh --ec2-ip <EC2_IP> --logs
# Expected: "Starting Slack router in Socket Mode …" then "Bolt app is running!"
```

> If `--logs` reports "No ai-slack-router deployment found", the deploy hasn't
> run yet — run the command above without `--logs` first.

Do Slack app setup (same as Track A Step A5) if not already done.

### B8. Connect each developer's ant worker (on each laptop)

Each team member runs this once on their own machine:

```bash
cd ~/workplace/formicary

export FORMICARY_TOKEN="<my-own-token from EC2 UI>"

./scripts/setup-ant-worker.sh \
  --queen EC2_IP \
  --token "$FORMICARY_TOKEN"
  # --user alice   (optional, defaults to $USER)
```

The script creates the `formicary-ant-credentials` secret, substitutes your
`QUEEN_HOST` and `USER_TAG` into `k8s/formicary-ant.yaml`, and applies it.
Your ant connects to EC2's WebSocket endpoint `ws://EC2_IP:7777/ws/queue`.

Verify in the Formicary dashboard (`http://EC2_IP:7777/dashboard/ants`):
your ant should appear with tag `user=<your-username>`.

Each person then uploads their own workflow configs:

```bash
cd ~/workplace/formicary/docs/examples
export FORMICARY_URL="http://EC2_IP:7777"
export FORMICARY_TOKEN="<my-token>"

./deploy-ai-workflows.sh \
  --server "$FORMICARY_URL" \
  --set-configs \
  --gh-org YOUR_ORG \
  --gh-repo YOUR_REPO \
  --ant-user-tag "${USER}"
```

This sets your personal `UserTag` org config. All your Slack-submitted jobs
will now run exclusively on your ant worker.

---

## Update workflow: after code changes

### After changing formicary Go code

```bash
# On your laptop:
cd ~/workplace/formicary
make test        # must pass
make docker-build
docker push plexobject/formicary:latest

# On EC2 (or via kubectl with correct context):
kubectl rollout restart deployment/formicary
kubectl rollout status deployment/formicary
```

### After changing ai-dev-tools Python code

```bash
# On your laptop:
cd ~/workplace/ai-dev-tools
make test        # must pass
make build
docker push plexobject/ai-dev-tools:latest

# Restart on EC2 (via SSH kubectl wrapper):
./deploy-ai-slack-router.sh --ec2-ip <EC2_IP>
# or just restart the deployment:
# ssh -i ~/ec2-key.pem ec2-user@<EC2_IP> \
#   "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout restart deployment/ai-slack-router"
```

### After changing workflow YAMLs (no image rebuild needed)

```bash
cd ~/workplace/formicary/docs/examples
export FORMICARY_URL="http://EC2_IP:7777"
export FORMICARY_TOKEN="<token>"

./deploy-ai-workflows.sh --server "$FORMICARY_URL"
# or for Jira:
./deploy-ai-jira-workflows.sh --server "$FORMICARY_URL"
```

### Change any single org config

```bash
cd ~/workplace/ai-dev-tools
python -m scripts.slack.deploy_workflows \
  --server "$FORMICARY_URL" \
  --set-config BASE_BRANCH stage

# Multiple at once (repeatable):
python -m scripts.slack.deploy_workflows \
  --server "$FORMICARY_URL" \
  --set-config BASE_BRANCH stage \
  --set-config UserTag alice
```

---

## Manifest / script reference

| File | Where to run | What it does |
|------|-------------|-------------|
| `formicary/k8s/formicary-all-in-one.yaml` | Local k8s | Leader + embedded ant, single pod |
| `formicary/k8s/formicary-leader.yaml` | EC2 k8s | Leader only, no embedded ant |
| `formicary/k8s/formicary-ant.yaml` | Laptop k8s (via setup script) | Per-user ant worker template |
| `formicary/scripts/setup-ant-worker.sh` | Each developer's laptop | Renders + applies ant YAML |
| `formicary/docs/examples/deploy-ai-workflows.sh` | Any machine with `FORMICARY_URL` | Upload GitHub workflows + set org configs |
| `formicary/docs/examples/deploy-ai-jira-workflows.sh` | Any machine with `FORMICARY_URL` | Upload Jira workflows + set org configs |
| `formicary/docs/examples/deploy-ai-slack-router.sh` | EC2 (or laptop for local) | Create router secret + apply router k8s manifest |
| `formicary/docs/examples/ai-slack-router-k8s.yaml` | Applied by deploy script | Router Deployment definition |
| `ai-dev-tools/scripts/slack/deploy_workflows.py` | Any machine | `--set-config KEY VALUE` for individual org configs |

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `make test` fails in formicary | Fix Go tests before doing anything else — never deploy broken code |
| `make test` fails in ai-dev-tools | Fix Python tests before deploying |
| formicary pod crashloops | `kubectl logs deployment/formicary` — usually missing secret or YAML syntax |
| Slack router not connecting | `kubectl logs deployment/ai-slack-router` — check `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` in `ai-slack-router-credentials` secret |
| Bot in Slack doesn't respond | Run `/invite @your-bot-name` in the channel — bot must be invited |
| `@bot qjira ...` not found | `qjira` was removed — use `jira-query`: `@bot jira-query <term>` |
| Jobs don't route to my ant | Check `UserTag` org config matches your ant's `user=` label. Run `kubectl logs deployment/ai-slack-router \| grep UserTag` |
| Branch from wrong base | Set `BASE_BRANCH`: `python -m scripts.slack.deploy_workflows --set-config BASE_BRANCH stage` |
| Port-forward drops on EC2 | Use `nohup` or a systemd service; or configure a proper NodePort/LoadBalancer |
| TLS: `x509: certificate` errors | Self-signed cert with nip.io — deploy scripts auto-add `-k`; browser: accept the warning on first visit |
| OAuth keeps redirecting to dashboard | Cert SAN must include `<ip>.nip.io`; iptables DNAT must exclude pod CIDR (`! -s 10.42.0.0/16`) |
| `--logs` shows "No deployment found" | Run deploy first (without `--logs`), then check logs |
| `kubectl: command not found` on EC2 | k3s uses `/usr/local/bin/kubectl`; the SSH wrapper sets `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` automatically |
| Ant pod can't reach EC2 | Check EC2 security group has port 7777 open; `kubectl exec -it <ant-pod> -- curl http://formicary:7777/api/jobs/definitions` |

---

## See Also

- [Slack App Setup](slack-setup.md) — full OAuth scopes, Socket Mode, event subscriptions
- [Slack Router — How It Works](slack-router.md) — routing, channel auto-derive, extensibility, org configs
- [System Reference](system-reference.md) — all org config keys and their defaults
- [formicary/docs/ant-worker-setup.md](../../formicary/docs/ant-worker-setup.md) — developer onboarding (shorter version of B8)
