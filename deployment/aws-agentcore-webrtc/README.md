# Amazon Bedrock AgentCore Runtime WebRTC Example

This example demonstrates how to deploy a Pipecat voice agent to **Amazon Bedrock AgentCore Runtime** using SmallWebRTC as a lightweight transport mechanism. The example pipeline orchestrates Deepgram (speech-to-text), Amazon Nova (LLM), and Cartesia (text-to-speech).

## Prerequisites

- Accounts with:
  - AWS
  - Deepgram
  - Cartesia
- Python 3.10 or higher
- `uv` package manager

## Set Up the Environment

### IAM Configuration

Configure your IAM user with the necessary policies for AgentCore deployment:

- `BedrockAgentCoreFullAccess`
- A new policy (maybe named `BedrockAgentCoreCLI`) configured [like this](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html#runtime-permissions-starter-toolkit)

You can also choose to specify more granular permissions; see [Amazon Bedrock AgentCore docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html) for more information.

To authenticate with AWS, you have two options:

1. Export environment variables:

   ```bash
   export AWS_SECRET_ACCESS_KEY=your_secret_key
   export AWS_ACCESS_KEY_ID=your_access_key
   export AWS_REGION=your_region
   export AWS_DEFAULT_REGION=your_default_region
   ```

2. Or use AWS CLI configuration:
   ```bash
   aws configure
   ```
   This will create/update your AWS credentials file (~/.aws/credentials).

### Virtual Environment Setup

Create and activate a virtual environment:

```bash
uv sync
```

### Environment Variables Configuration

1. For the agent:

   ```bash
   cd agent
   cp env.example .env
   ```

   Add your API keys:

   - `AWS_ACCESS_KEY_ID`: Your AWS access key ID for the Amazon Bedrock LLM used by the agent
   - `AWS_SECRET_ACCESS_KEY`: Your AWS secret access key for the Amazon Bedrock LLM used by the agent
   - `AWS_REGION`: The AWS region for the Amazon Bedrock LLM used by the agent
   - `DEEPGRAM_API_KEY`: Your Deepgram API key
   - `CARTESIA_API_KEY`: Your Cartesia API key
   - **TURN Server Configuration** - see [TURN Credential Management](#turn-credential-management) below

   > Important Notes about TURN Server Configuration:
   >
   > **VPC Mode (recommended):**
   > - Both TCP and UDP TURN are supported via NAT Gateway
   > - UDP (recommended): `turn:server.example.com:80`
   > - TCP: `turn:server.example.com:80?transport=tcp`
   >
   > **PUBLIC Mode:**
   > - Only TCP TURN is supported - use `turn:server.example.com:80?transport=tcp`
   > - UDP connections are blocked

2. For the server:
   ```bash
   cd server
   cp env.example .env
   ```
   The server configuration is minimal - the `AGENT_RUNTIME_ARN` will be automatically set during agent deployment.

## Agent Configuration

Configure your bot as an AgentCore agent:

```bash
./scripts/configure.sh
```

This script automatically:
1. Creates IAM execution role (if needed)
2. Configures container deployment with docker runtime
3. Patches Dockerfile to add SmallWebRTC dependencies (`libgl1` and `libglib2.0-0`)

> Technical Note:
> Direct Code Deploy isn't used because some dependencies (like `numba`) lack `aarch64_manylinux2014` wheels.

## ICE/TURN Credential Management

This example provides automatic TURN credential rotation with support for multiple providers (Cloudflare, Twilio, static).

**How it works:**
- **Intermediary server** refreshes credentials automatically (default: 5 minutes before 24h expiry)
- **AWS Secrets Manager** stores current credentials
- **AgentCore runtime** fetches fresh credentials for each WebRTC connection

### Quick Start

#### Option 1: Cloudflare Calls (Recommended)

1. Get credentials from [Cloudflare Calls](https://developers.cloudflare.com/calls/) (create TURN Key)

2. Configure in `server/.env`:
   ```bash
   ICE_SERVER_PROVIDER=cloudflare
   ICE_SERVER_KEY_ID=your_turn_key_id
   ICE_SERVER_API_TOKEN=your_api_token
   ```

3. Configure in `agent/.env`:
   ```bash
   ICE_SERVER_PROVIDER=cloudflare
   ICE_SERVER_CREDENTIALS_SECRET=ice-server-credentials
   ```

4. Initialize:
   ```bash
   ./scripts/setup-turn-secrets.sh
   ./scripts/setup-iam-role.sh
   ```

#### Option 2: Twilio TURN Service

1. Get credentials from [Twilio Console](https://www.twilio.com/) (Account SID + Auth Token)

2. Configure `server/.env`:
   ```bash
   ICE_SERVER_PROVIDER=twilio
   ICE_SERVER_KEY_ID=your_account_sid
   ICE_SERVER_API_TOKEN=your_auth_token
   ```

3. Same agent/.env as Cloudflare, then run setup scripts above

#### Option 3: Static Credentials (Dev/Testing)

Configure in both `agent/.env` and `server/.env`:
```bash
ICE_SERVER_PROVIDER=static
ICE_SERVER_URLS=turn:global.relay.metered.ca:80
ICE_SERVER_USERNAME=your_username
ICE_SERVER_CREDENTIAL=your_password
```

No Secrets Manager or IAM setup required.

### Configuration Reference

**Required in both `.env` files:**
```bash
ICE_SERVER_PROVIDER=cloudflare  # cloudflare, twilio, or static
AWS_REGION=us-east-1
ICE_SERVER_CREDENTIALS_SECRET=ice-server-credentials
```

**Optional tuning:**
```bash
ICE_SERVER_REFRESH_BUFFER=300  # Refresh N seconds before expiry
ICE_SERVER_TTL=86400           # Credential lifetime (24 hours)
```

**Provider credentials (in `server/.env` only):**
```bash
# Cloudflare or Twilio (unified):
ICE_SERVER_KEY_ID=your_key_or_account_sid
ICE_SERVER_API_TOKEN=your_token

# Static (in both .env files):
ICE_SERVER_URLS=turn:server:80,turns:server:443
ICE_SERVER_USERNAME=username
ICE_SERVER_CREDENTIAL=password
```

### Troubleshooting

**"Access denied to secret":**
- Run `./scripts/setup-iam-role.sh`
- Verify: `aws secretsmanager describe-secret --secret-id ice-server-credentials`

**"Failed to refresh credentials":**
- Check provider credentials in `server/.env`
- Verify internet connectivity from intermediary server
- Server continues using cached credentials during outages

**WebRTC connection fails:**
- Test TURN manually: [Trickle ICE](https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/)
- Check server logs for credential refresh status
- For static provider: verify `ICE_SERVER_PROVIDER=static` in both `.env` files

### Architecture Notes

**Shared Module Deployment:**
The `shared/` directory contains credential management code used by both server and agent. For AgentCore deployment, this directory is copied into `agent/shared/` before building the container. Alternative approaches for production:
- **Python package**: Make `shared/` an installable package with `setup.py`
- **Symlinks**: Use `ln -s ../shared agent/shared` (requires Docker COPY --follow-symlinks)
- **Monorepo**: Adjust build context to include parent directory

**Security:**
- Credentials stored encrypted in AWS Secrets Manager
- AgentCore IAM role has read-only access (`secretsmanager:GetSecretValue`)
- Short-lived tokens (24h) with proactive refresh (5min buffer)
- No credentials in code or version control

## ⚠️ Before Proceeding

Just in case you've previously deployed other agents to AgentCore, ensure that you have the desired agent selected as "default" in the `agentcore` tool:

```
# Check
uv run agentcore configure list
# Set
uv run agentcore configure set-default <agent-name>
```

The following steps act on `agentcore`'s default agent.

## Deployment to AgentCore Runtime

**VPC Mode (recommended) - TCP and UDP TURN support:**

```bash
# First time: Create VPC infrastructure (NAT Gateway costs ~$32/month)
./scripts/setup-vpc.sh

# Deploy agent
./scripts/launch.sh
```

This deploys AgentCore Runtime in private subnets with NAT Gateway for outbound internet access, enabling UDP TURN relay (blocked in PUBLIC mode) for better WebRTC connection reliability, lower latency, and enhanced security with private subnet isolation.

**Infrastructure overview:**
- VPC with public and private subnets across 2 availability zones
- Internet Gateway for public subnet connectivity
- NAT Gateway in public subnet for private subnet outbound traffic
- Route tables directing private subnet traffic through NAT Gateway
- Security groups allowing outbound HTTPS and UDP connections

**PUBLIC Mode - TCP TURN only:**

For development/testing without UDP TURN:

```bash
./scripts/launch.sh
```

The launch script:
1. Reads environment variables from `agent/.env`
2. Deploys to AgentCore
3. Updates the server's configuration with the agent ARN
4. Displays log-tailing commands for monitoring

## Running on AgentCore Runtime

1. Start the server:

   ```bash
   cd server
   uv run server.py
   ```

2. Access the UI:
   - Open http://localhost:7860 in your browser
   - Or use your configured custom port

3. Test WebRTC connectivity:
   - Click "Connect" in the UI
   - Allow microphone permissions when prompted
   - Speak to the agent - you should hear a voice response
   - Verify connection type:
     - Open browser DevTools (F12 → Console tab)
     - Type `chrome://webrtc-internals` in address bar (Chrome) or `about:webrtc` (Firefox) for detailed stats
     - Look for "Selected candidate pair" showing protocol (`udp` for VPC, `tcp` for PUBLIC) and type (`relay` for TURN)
   - For log monitoring, see the next section below

## Monitoring and Troubleshooting

### View Intermediary Server Logs

The intermediary server (`server.py`) proxies WebRTC signaling between the browser client and AgentCore Runtime. Check the terminal where the server is already running (from step 1 above).

Look for:
- WebRTC SDP offers and answers
- ICE candidate exchanges showing protocol (`udp`/`tcp`) and type (`relay`/`host`)
- Connection events and errors

### View Agent Logs

Use the log-tailing command provided during deployment:

```bash
# Replace with your actual command
aws logs tail /aws/bedrock-agentcore/runtimes/bot1-0uJkkT7QHC-DEFAULT --log-stream-name-prefix "2025/11/19/[runtime-logs]" --follow
```

## Test Agent Manually

Test the agent using the AWS CLI:

```bash
uv run agentcore invoke \
  --session-id user-123456-conversation-12345679 \
  '{
  "sdp": "YOUR_OFFER",
  "type": "offer"
}'
```

> This will only allow you to see that the Pipecat agent has started, but you won’t be able to hear or send audio. So it is only useful for troubleshooting.

## Cleanup

Remove your agent:

```bash
./scripts/destroy.sh
```

If using VPC mode, remove VPC resources:

```bash
./scripts/cleanup-vpc.sh
```

## Local Development

Run your bot locally for testing:

```bash
PIPECAT_LOCAL_DEV=1 uv run pipecat-agent.py
```

## Additional Resources

- [Amazon Bedrock AgentCore Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [TURN Server Configuration Guide](https://webrtc.org/getting-started/turn-server)
