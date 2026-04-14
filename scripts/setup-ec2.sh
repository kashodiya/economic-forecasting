#!/usr/bin/env bash
set -euo pipefail

# Idempotent EC2 setup script
# Installs: Docker, Docker Compose, Node.js (LTS)

echo "=== Starting EC2 setup ==="

# Update system packages
sudo yum update -y

# --- Docker ---
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  sudo yum install -y docker
else
  echo "Docker already installed, skipping."
fi

sudo systemctl enable docker
sudo systemctl start docker

# Add ec2-user to docker group (idempotent)
if ! groups ec2-user | grep -q '\bdocker\b'; then
  sudo usermod -aG docker ec2-user
  echo "Added ec2-user to docker group (re-login required)."
fi

# --- Docker Compose ---
COMPOSE_VERSION="v2.29.2"
COMPOSE_DEST="/usr/local/lib/docker/cli-plugins/docker-compose"

if ! docker compose version &>/dev/null; then
  echo "Installing Docker Compose ${COMPOSE_VERSION}..."
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    -o "${COMPOSE_DEST}"
  sudo chmod +x "${COMPOSE_DEST}"
else
  echo "Docker Compose already installed, skipping."
fi

# --- Node.js (LTS via nvm) ---
NVM_DIR="/home/ec2-user/.nvm"

if [ ! -d "$NVM_DIR" ]; then
  echo "Installing nvm for ec2-user..."
  sudo -u ec2-user bash -c 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash'
fi

# Install node as ec2-user
if ! sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && command -v node' &>/dev/null; then
  echo "Installing Node.js LTS..."
  sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && nvm install --lts && nvm alias default lts/*'
else
  echo "Node.js already installed: $(sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && node --version'), skipping."
fi

# --- code-server ---
export HOME="${HOME:-/root}"
if ! command -v code-server &>/dev/null; then
  echo "Installing code-server..."
  curl -fsSL https://code-server.dev/install.sh | sh
else
  echo "code-server already installed, skipping."
fi

# Fetch shared password from Secrets Manager
APP_PASSWORD=$(aws secretsmanager get-secret-value --secret-id economic-forecasting/app-password --query SecretString --output text --region ${AWS_REGION:-us-east-1})

# Configure code-server for ec2-user
CS_CONFIG="/home/ec2-user/.config/code-server/config.yaml"
sudo -u ec2-user mkdir -p /home/ec2-user/.config/code-server
echo "Configuring code-server..."
sudo -u ec2-user bash -c "cat > $CS_CONFIG" <<CSEOF
bind-addr: 0.0.0.0:8443
auth: password
password: ${APP_PASSWORD}
cert: false
CSEOF

# Enable and start code-server as ec2-user
sudo systemctl enable --now code-server@ec2-user

# --- JupyterLab ---
# Ensure pip is available
if ! sudo -u ec2-user python3 -m pip --version &>/dev/null; then
  echo "Installing pip..."
  sudo yum install -y python3-pip
fi

if ! sudo -u ec2-user python3 -m pip show jupyterlab &>/dev/null; then
  echo "Installing JupyterLab..."
  sudo -u ec2-user python3 -m pip install --user jupyterlab
else
  echo "JupyterLab already installed, skipping."
fi

# Create systemd service for JupyterLab
JUPYTER_SERVICE="/etc/systemd/system/jupyterlab.service"
echo "Creating JupyterLab systemd service..."
cat > "$JUPYTER_SERVICE" <<'JEOF'
[Unit]
Description=JupyterLab
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user
ExecStart=/home/ec2-user/.local/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser
Restart=on-failure

[Install]
WantedBy=multi-user.target
JEOF
systemctl daemon-reload

# Configure JupyterLab password
echo "Configuring JupyterLab password..."
JUPYTER_HASH=$(sudo -u ec2-user python3 -c "from jupyter_server.auth import passwd; print(passwd('${APP_PASSWORD}'))")
sudo -u ec2-user mkdir -p /home/ec2-user/.jupyter
sudo -u ec2-user bash -c "cat > /home/ec2-user/.jupyter/jupyter_server_config.py" <<JPEOF
c.ServerApp.password = '${JUPYTER_HASH}'
c.ServerApp.token = ''
JPEOF

sudo systemctl enable --now jupyterlab
sudo systemctl restart code-server@ec2-user
sudo systemctl restart jupyterlab

# --- Team Workspaces ---
echo "Creating team folders..."
for user in kaushik alouki jasveen kalyan julia xiaoyu ashley; do
  sudo -u ec2-user mkdir -p /home/ec2-user/$user
done

# --- Claude Code ---
if ! sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && command -v claude' &>/dev/null; then
  echo "Installing Claude Code..."
  sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && curl -fsSL https://claude.ai/install.sh | bash'
else
  echo "Claude Code already installed, skipping."
fi

# Configure Claude Code env for ec2-user
CLAUDE_ENV_FILE="/home/ec2-user/.claude_env"
sudo -u ec2-user bash -c "cat > $CLAUDE_ENV_FILE" <<'CLEOF'
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
export ANTHROPIC_MODEL='us.anthropic.claude-3-5-sonnet-20241022-v2:0'
CLEOF

# Source it from bashrc if not already
if ! sudo -u ec2-user grep -q '.claude_env' /home/ec2-user/.bashrc 2>/dev/null; then
  echo '[ -f ~/.claude_env ] && source ~/.claude_env' | sudo -u ec2-user tee -a /home/ec2-user/.bashrc > /dev/null
fi

echo "=== EC2 setup complete ==="
echo "Docker:  $(docker --version)"
echo "Compose: $(docker compose version)"
echo "Node.js: $(sudo -u ec2-user bash -c 'source /home/ec2-user/.nvm/nvm.sh && node --version')"
echo ""
echo "NOTE: Log out and back in for docker group changes to take effect."
