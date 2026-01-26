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

## TURN Credential Management

This example includes a flexible, provider-agnostic TURN credential management system with automatic rotation for secure WebRTC connectivity.

### Supported Providers

#### 1. Cloudflare Calls (Recommended - Default)

**Features:**
- Automatic credential generation with short-lived tokens (configurable TTL, default: 24h)
- Multiple transport options (UDP, TCP, TLS)
- Multiple ports for firewall flexibility (3478, 80, 443, 5349)
- Global infrastructure with low latency

**Setup:**

1. Get Cloudflare TURN credentials:
   - Sign up at [Cloudflare Calls](https://developers.cloudflare.com/calls/)
   - Create a TURN Key and obtain:
     - `TURN_KEY_ID`
     - `TURN_API_TOKEN`

2. Configure in `server/.env` and `agent/.env`:
   ```bash
   TURN_PROVIDER=cloudflare
   CLOUDFLARE_TURN_KEY_ID=your_turn_key_id
   CLOUDFLARE_TURN_API_TOKEN=your_api_token
   TURN_TTL=86400  # 24 hours (recommended)
   ```

3. Initialize AWS Secrets Manager:
   ```bash
   ./scripts/setup-turn-secrets.sh
   ```

4. Update IAM permissions (if not already done):
   ```bash
   ./scripts/setup-iam-role.sh
   ```

**How it works:**
- **Intermediary server** (`server/server.py`): Generates fresh Cloudflare credentials on startup and refreshes them automatically 5 minutes before expiry
- **AWS Secrets Manager**: Stores current credentials, updated by intermediary server
- **AgentCore runtime** (`agent/pipecat-agent.py`): Fetches fresh credentials from Secrets Manager for each WebRTC connection

#### 2. Twilio TURN Service

**Setup:**

1. Get Twilio credentials:
   - Sign up at [Twilio](https://www.twilio.com/)
   - Get your Account SID and Auth Token from the console

2. Configure in `server/.env` and `agent/.env`:
   ```bash
   TURN_PROVIDER=twilio
   TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxx
   TWILIO_AUTH_TOKEN=your_auth_token
   TURN_TTL=86400
   ```

3. Initialize Secrets Manager and update IAM (same as Cloudflare above)

#### 3. Static Credentials (Backward Compatible)

For development, testing, or TURN services without dynamic credential APIs.

**Setup:**

Configure in `agent/.env` and `server/.env`:
```bash
TURN_PROVIDER=static
ICE_SERVER_URLS=turn:global.relay.metered.ca:80
ICE_SERVER_USERNAME=your_username
ICE_SERVER_CREDENTIAL=your_password
```

**Note:** Static credentials don't require Secrets Manager and work immediately without additional setup.

### Configuration Reference

**Common Configuration (both `agent/.env` and `server/.env`):**

```bash
# Provider selection
TURN_PROVIDER=cloudflare  # Options: cloudflare (default), twilio, static

# AWS Configuration
AWS_REGION=us-east-1
TURN_CREDENTIALS_SECRET=turn-credentials  # Secrets Manager secret name

# Credential refresh timing
TURN_REFRESH_BUFFER=300  # Refresh 5 minutes before expiry (seconds)
TURN_TTL=86400           # Credential lifetime: 24 hours (seconds)
```

**Cloudflare-specific (`server/.env` only):**
```bash
CLOUDFLARE_TURN_KEY_ID=your_turn_key_id
CLOUDFLARE_TURN_API_TOKEN=your_api_token
```

**Twilio-specific (`server/.env` only):**
```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
```

**Static provider (`agent/.env` and `server/.env`):**
```bash
ICE_SERVER_URLS=turn:server.example.com:80,turn:server.example.com:443
ICE_SERVER_USERNAME=your_username
ICE_SERVER_CREDENTIAL=your_password
```

### Architecture

**System Components:**

1. **Intermediary Server** (local, `server/server.py`):
   - Runs credential manager with background refresh loop
   - Generates fresh credentials from provider API (Cloudflare/Twilio)
   - Updates AWS Secrets Manager with new credentials
   - Returns credentials to browser clients via `/start` endpoint

2. **AWS Secrets Manager**:
   - Centralized, secure credential storage
   - Updated by intermediary server when credentials refresh
   - Read by AgentCore runtime for each connection

3. **AgentCore Runtime** (AWS, `agent/pipecat-agent.py`):
   - Fetches fresh credentials from Secrets Manager
   - Creates WebRTC connections with current credentials
   - Automatic failover to cached credentials if Secrets Manager unavailable

**Credential Lifecycle:**

```
[Provider API]
     ↓ (generate every 24h)
[Intermediary Server]
     ↓ (update)
[AWS Secrets Manager]
     ↓ (read)
[AgentCore Runtime] → [WebRTC Connection]
```

### Troubleshooting

**"Credential manager not initialized" error:**
- Check `server/.env` has correct `TURN_PROVIDER` and provider credentials
- Verify provider credentials are valid (test API access)
- Check server logs for initialization errors

**"Access denied to secret" error:**
- Run `./scripts/setup-iam-role.sh` to add Secrets Manager permissions
- Verify IAM role has `secretsmanager:GetSecretValue` permission
- Check secret exists: `aws secretsmanager describe-secret --secret-id turn-credentials`

**"Failed to refresh credentials" error:**
- Check internet connectivity from intermediary server
- Verify provider API credentials are correct
- Check provider API status/rate limits
- Server will continue using cached credentials temporarily

**WebRTC connection fails with relay candidates:**
- Verify TURN credentials are not expired
- Check server logs for credential refresh status
- Test TURN server manually using [Trickle ICE test](https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/)
- For Cloudflare: Verify TURN Key has correct permissions

**Static provider credentials not working:**
- Set `TURN_PROVIDER=static` in both `.env` files
- Verify `ICE_SERVER_URLS`, `ICE_SERVER_USERNAME`, `ICE_SERVER_CREDENTIAL` are set
- Test credentials with [Trickle ICE](https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/)

### Security Best Practices

1. **Short-lived credentials**: Use 24h or less TTL for dynamic providers
2. **Proactive refresh**: Credentials refresh 5 minutes before expiry (no connection disruption)
3. **Secrets Manager**: All production credentials stored encrypted in AWS Secrets Manager
4. **IAM least privilege**: AgentCore role has read-only access to secrets
5. **No credentials in code**: All credentials loaded from environment variables
6. **VPC deployment**: Use private subnets with NAT gateway for enhanced security

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
