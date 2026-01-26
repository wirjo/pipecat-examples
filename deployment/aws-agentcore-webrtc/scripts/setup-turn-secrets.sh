#!/bin/bash

###############################################
# Setup TURN Credentials in AWS Secrets Manager
#
# This script initializes AWS Secrets Manager with
# TURN credentials that can be used by AgentCore runtime.
#
# Usage:
#   ./scripts/setup-turn-secrets.sh
#
# Prerequisites:
#   - AWS CLI configured with credentials
#   - IAM permissions for Secrets Manager (create/update)
#   - .env file with TURN configuration
###############################################

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "TURN Credentials Setup for AWS Secrets Manager"
echo "=========================================="
echo ""

###############################################
# Load configuration from .env
###############################################

# Check if .env exists
if [ ! -f "server/.env" ]; then
    echo -e "${RED}❌ Error: server/.env file not found${NC}"
    echo "Please create server/.env with TURN configuration"
    exit 1
fi

# Load environment variables
echo "📋 Loading configuration from server/.env..."
source server/.env

# Set defaults
AWS_REGION=${AWS_REGION:-us-east-1}
ICE_SERVER_PROVIDER=${ICE_SERVER_PROVIDER:-cloudflare}
ICE_SERVER_CREDENTIALS_SECRET=${ICE_SERVER_CREDENTIALS_SECRET:-ice-server-credentials}

echo "   Region: $AWS_REGION"
echo "   Provider: $ICE_SERVER_PROVIDER"
echo "   Secret Name: $ICE_SERVER_CREDENTIALS_SECRET"
echo ""

###############################################
# Validate AWS credentials
###############################################

echo "🔐 Validating AWS credentials..."
if ! aws sts get-caller-identity > /dev/null 2>&1; then
    echo -e "${RED}❌ Error: AWS credentials not configured${NC}"
    echo "Please configure AWS CLI with: aws configure"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "   Account ID: $ACCOUNT_ID"
echo ""

###############################################
# Check if secret already exists
###############################################

echo "🔍 Checking if secret exists..."
SECRET_EXISTS=false
if aws secretsmanager describe-secret --secret-id "$ICE_SERVER_CREDENTIALS_SECRET" --region "$AWS_REGION" > /dev/null 2>&1; then
    SECRET_EXISTS=true
    echo -e "${YELLOW}   ⚠️  Secret already exists: $ICE_SERVER_CREDENTIALS_SECRET${NC}"
    echo ""
    read -p "   Do you want to update the existing secret? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "   Exiting without changes."
        exit 0
    fi
else
    echo "   Secret does not exist, will create new secret"
fi
echo ""

###############################################
# Generate or fetch credentials based on provider
###############################################

echo "🔑 Preparing TURN credentials..."

if [ "$ICE_SERVER_PROVIDER" = "static" ]; then
    # Use static credentials from .env
    echo "   Using static credentials from .env"

    if [ -z "$ICE_SERVER_URLS" ] || [ -z "$ICE_SERVER_USERNAME" ] || [ -z "$ICE_SERVER_CREDENTIAL" ]; then
        echo -e "${RED}❌ Error: Static provider requires ICE_SERVER_URLS, ICE_SERVER_USERNAME, and ICE_SERVER_CREDENTIAL${NC}"
        exit 1
    fi

    # Convert comma-separated URLs to JSON array
    IFS=',' read -ra URL_ARRAY <<< "$ICE_SERVER_URLS"
    URLS_JSON="["
    for i in "${!URL_ARRAY[@]}"; do
        URL_ARRAY[$i]=$(echo "${URL_ARRAY[$i]}" | xargs)  # Trim whitespace
        URLS_JSON+="\"${URL_ARRAY[$i]}\""
        if [ $i -lt $((${#URL_ARRAY[@]} - 1)) ]; then
            URLS_JSON+=","
        fi
    done
    URLS_JSON+="]"

    # Create secret value
    SECRET_VALUE=$(cat <<EOF
{
  "urls": $URLS_JSON,
  "username": "$ICE_SERVER_USERNAME",
  "credential": "$ICE_SERVER_CREDENTIAL",
  "provider": "static",
  "expires_at": null,
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
)

elif [ "$ICE_SERVER_PROVIDER" = "cloudflare" ]; then
    echo "   Using Cloudflare provider (will be managed by intermediary server)"
    echo -e "${YELLOW}   ⚠️  For Cloudflare provider, credentials will be generated dynamically${NC}"
    echo "   Creating placeholder secret that will be updated by the server..."

    # Create placeholder secret
    SECRET_VALUE=$(cat <<EOF
{
  "urls": ["stun:stun.cloudflare.com:3478"],
  "username": "placeholder",
  "credential": "placeholder",
  "provider": "cloudflare",
  "expires_at": null,
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "note": "This will be replaced by dynamically generated credentials from the intermediary server"
}
EOF
)

elif [ "$ICE_SERVER_PROVIDER" = "twilio" ]; then
    echo "   Using Twilio provider (will be managed by intermediary server)"
    echo -e "${YELLOW}   ⚠️  For Twilio provider, credentials will be generated dynamically${NC}"
    echo "   Creating placeholder secret that will be updated by the server..."

    # Create placeholder secret
    SECRET_VALUE=$(cat <<EOF
{
  "urls": ["stun:global.stun.twilio.com:3478"],
  "username": "placeholder",
  "credential": "placeholder",
  "provider": "twilio",
  "expires_at": null,
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "note": "This will be replaced by dynamically generated credentials from the intermediary server"
}
EOF
)

else
    echo -e "${RED}❌ Error: Unknown TURN provider: $ICE_SERVER_PROVIDER${NC}"
    echo "Supported providers: cloudflare, twilio, static"
    exit 1
fi

echo ""

###############################################
# Create or update secret
###############################################

echo "💾 Saving credentials to Secrets Manager..."

if [ "$SECRET_EXISTS" = true ]; then
    # Update existing secret
    aws secretsmanager put-secret-value \
        --secret-id "$ICE_SERVER_CREDENTIALS_SECRET" \
        --secret-string "$SECRET_VALUE" \
        --region "$AWS_REGION" > /dev/null

    echo -e "${GREEN}✅ Secret updated successfully${NC}"
else
    # Create new secret
    aws secretsmanager create-secret \
        --name "$ICE_SERVER_CREDENTIALS_SECRET" \
        --description "TURN server credentials for WebRTC connections (provider: $ICE_SERVER_PROVIDER)" \
        --secret-string "$SECRET_VALUE" \
        --region "$AWS_REGION" > /dev/null

    echo -e "${GREEN}✅ Secret created successfully${NC}"
fi

echo ""

###############################################
# Validate IAM permissions
###############################################

echo "🔒 Validating IAM permissions..."

# Check if AgentCore execution role exists
ROLE_NAME="AgentCoreRuntimeRole"
if aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
    echo "   ✅ IAM role exists: $ROLE_NAME"

    # Check if role has Secrets Manager permissions
    POLICY_NAME="AgentCoreRuntimePolicy"
    POLICY_DOC=$(aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" 2>/dev/null || echo "")

    if echo "$POLICY_DOC" | grep -q "secretsmanager:GetSecretValue"; then
        echo "   ✅ IAM role has Secrets Manager read permissions"
    else
        echo -e "${YELLOW}   ⚠️  IAM role may be missing Secrets Manager permissions${NC}"
        echo "   Run: ./scripts/setup-iam-role.sh to add required permissions"
    fi
else
    echo -e "${YELLOW}   ⚠️  IAM role not found: $ROLE_NAME${NC}"
    echo "   Run: ./scripts/setup-iam-role.sh to create the role"
fi

echo ""

###############################################
# Summary
###############################################

echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Secret Details:"
echo "  Name: $ICE_SERVER_CREDENTIALS_SECRET"
echo "  Region: $AWS_REGION"
echo "  Provider: $ICE_SERVER_PROVIDER"
echo "  ARN: arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT_ID:secret:$ICE_SERVER_CREDENTIALS_SECRET"
echo ""
echo "Next Steps:"
echo "  1. Ensure IAM role has Secrets Manager permissions (run setup-iam-role.sh)"
echo "  2. Deploy AgentCore runtime: ./scripts/launch.sh"

if [ "$ICE_SERVER_PROVIDER" != "static" ]; then
    echo "  3. Start intermediary server: cd server && python server.py"
    echo "     (Server will automatically refresh credentials in Secrets Manager)"
fi

echo ""
echo "To verify the secret:"
echo "  aws secretsmanager get-secret-value --secret-id $ICE_SERVER_CREDENTIALS_SECRET --region $AWS_REGION --query SecretString --output text | jq"
echo ""
