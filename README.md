# Micro-Pockets

AI-powered expense tracker that lives inside WhatsApp. No app to download. Just WhatsApp.

Built for the Google Rapid Agent Hackathon using Gemini AI on Vertex AI, MongoDB Atlas, and Google Cloud Run.

## Live Demo

- **Live App:** https://micropockets-183402826410.us-central1.run.app
- **MongoDB MCP Demo:** https://micropockets-183402826410.us-central1.run.app/demo/mcp
- **DB Health:** https://micropockets-183402826410.us-central1.run.app/test-db
- **Demo Video:** https://youtu.be/h2RgMy-XRvk

## Test the App on WhatsApp

The app is currently in development mode. To test it live on WhatsApp:

1. **Send your WhatsApp number** to najamhere4@gmail.com with subject "Micro-Pockets Judge Access"
2. Your number will be added as a test user within a few hours
3. Then message **+1 (555) 653-2901** on WhatsApp to start

Or **watch the full demo video** to see every feature in action:
https://youtu.be/h2RgMy-XRvk

### Quick test commands once approved:
```
Hi                          — start onboarding
balance                     — check your pockets
spent 500 on food           — log an expense
how am I doing              — monthly summary
how should I spend my money — 50/30/20 advice
Apple stock price           — live stock price
help                        — full feature guide
```

## Features

- Automatic bank SMS capture on Android (MacroDroid) and iOS (Shortcuts)
- Log expenses by text or voice note in any language
- Budget pockets with real-time balance tracking
- AI advisor with 50/30/20 framework
- Live stock prices — Pakistani and global
- Monthly summaries and spending insights
- Powered by Gemini 2.0 Flash on Vertex AI

## Tech Stack

- **Backend:** FastAPI + Python 3.11
- **AI:** Gemini 2.0 Flash via Vertex AI (Google SDK)
- **Database:** MongoDB Atlas + MongoDB MCP Server
- **Hosting:** Google Cloud Run
- **Interface:** WhatsApp Business API
- **Bank SMS:** MacroDroid (Android) + iOS Shortcuts

## Project Structure

```
Micro-Pockets/
├── main.py                    # FastAPI gateway, routing, onboarding
├── mcp_demo.py                # MongoDB MCP Server demo endpoint
├── requirements.txt
├── Dockerfile
├── agents/
│   ├── interpreter_agent.py   # Classifies every incoming message
│   ├── interaction_agent.py   # Handles conversation + pending states
│   ├── query_agent.py         # Balance, summaries, transactions
│   ├── ingestion_agent.py     # Logs expenses, maps merchants
│   ├── advisor_agent.py       # Proactive alerts + 50/30/20 analysis
│   ├── voice_agent.py         # Voice note transcription
│   └── stock_agent.py         # Live stock prices
└── core/
    ├── database.py            # MongoDB Atlas connection
    └── mcp_tools.py           # MongoDB aggregation pipelines
```

## Prerequisites

- Python 3.11+
- Google Cloud account with Vertex AI enabled
- MongoDB Atlas cluster
- WhatsApp Business API account (Meta Developer)
- MacroDroid (Android) or iOS Shortcuts for bank SMS

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/jeryrepo/Micro-Pockets.git
cd Micro-Pockets
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up environment variables

Create a `.env` file in the root directory:

```env
# WhatsApp Business API
WA_ACCESS_TOKEN=your_whatsapp_access_token
WA_PHONE_NUMBER_ID=your_phone_number_id
WA_VERIFY_TOKEN=your_verify_token

# Google Cloud / Vertex AI
GCP_PROJECT_ID=your_gcp_project_id
GCP_LOCATION=us-central1
GEMINI_MODEL=gemini-2.0-flash

# MongoDB Atlas
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/micropockets
MONGODB_CONNECTION_STRING=mongodb+srv://username:password@cluster.mongodb.net/micropockets

# App URLs
ANDROID_REDIRECT=https://play.google.com/store/apps/details?id=com.macrodroid.app
IOS_SHORTCUT_URL=https://www.icloud.com/shortcuts/your_shortcut_id
```

### 4. Set up Google Cloud Authentication

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Enable required APIs:

```bash
gcloud services enable aiplatform.googleapis.com
gcloud services enable run.googleapis.com
```

### 5. Run locally

```bash
uvicorn main:app --reload --port 8000
```

### 6. Expose locally for WhatsApp webhook (development)

```bash
# Install cloudflared
cloudflared tunnel --url http://localhost:8000
```

Use the generated URL as your WhatsApp webhook:
```
https://your-tunnel-url.trycloudflare.com/webhook/whatsapp
```

## Deploying to Google Cloud Run

```bash
gcloud run deploy micropockets \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --set-env-vars "WA_VERIFY_TOKEN=your_token,WA_PHONE_NUMBER_ID=your_id,GCP_PROJECT_ID=your_project,GCP_LOCATION=us-central1,GEMINI_MODEL=gemini-2.0-flash,MONGODB_URI=your_mongodb_uri,WA_ACCESS_TOKEN=your_wa_token,MONGODB_CONNECTION_STRING=your_mongodb_uri"
```

## WhatsApp Webhook Setup

1. Go to Meta Developer Dashboard
2. WhatsApp → Configuration → Webhook
3. Set Callback URL: `https://your-cloud-run-url/webhook/whatsapp`
4. Set Verify Token: same as `WA_VERIFY_TOKEN` in your env
5. Subscribe to `messages`

## Bank SMS Setup

### Android (MacroDroid)
1. Install MacroDroid from Play Store
2. Create a new Macro named "Bank Alerts"
3. Trigger: SMS/MMS Received — filter by your bank name (e.g. HBL, MCB, UBL)
4. Action: HTTP Request
   - URL: `https://your-app-url/webhook/bank-notifications`
   - Method: POST
   - Body: `{"user_phone": "+923xxxxxxxxx", "sms_body": "[SMS Body]"}`
5. Enable and save

### iOS (Shortcuts)
1. Open Shortcuts app → Automations
2. New Automation → Message received
3. Filter by bank sender name
4. Add action: Get contents of URL
   - URL: `https://your-app-url/webhook/bank-notifications`
   - Method: POST
   - Body: `{"user_phone": "+923xxxxxxxxx", "sms_body": "Shortcut Input"}`
5. Save without asking

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook/whatsapp` | POST | WhatsApp message webhook |
| `/webhook/bank-notifications` | POST | Bank SMS ingestion |
| `/test-db` | GET | MongoDB connection health check |
| `/demo/mcp` | GET | MongoDB MCP Server demo |
| `/demo/mcp/tools` | GET | List MCP Server tools |

## License

MIT License — see LICENSE file for details.