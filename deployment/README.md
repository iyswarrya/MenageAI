# Google Cloud Run Deployment Guide

This guide describes how to build, deploy, and reproduce the deployment of the Family Receipt Agent (`menageai`) on Google Cloud Run.

---

## 1. Prerequisites

Before deploying, ensure you have the following installed and configured:
1. **Google Cloud CLI (`gcloud`)** installed and authenticated.
2. An active **Google Cloud Project** with billing enabled.
3. Your terminal authenticated with your user account:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project YOUR_PROJECT_ID
   ```

---

## 2. Required Google Cloud APIs

Enable the following APIs in your Google Cloud Project:
```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    discoveryengine.googleapis.com \
    --project=YOUR_PROJECT_ID
```

* **`run.googleapis.com`**: Hosts the web application.
* **`cloudbuild.googleapis.com`**: Handles building container images from source.
* **`artifactregistry.googleapis.com`**: Stores the compiled Docker container.
* **`aiplatform.googleapis.com`**: Enables access to Gemini models via Vertex AI.
* **`discoveryengine.googleapis.com`**: Required for integration with Gemini Enterprise / Antigravity Playground.

---

## 3. Required IAM Permissions

For the application to call Vertex AI models (Gemini) and allow the Antigravity Playground to discover the agent, grant these permissions:

### A. Vertex AI Access for the Cloud Run Instance
Run the following to allow the Cloud Run runtime service account to call Vertex AI:
```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### B. Gemini Enterprise Discovery Access
Allow the Discovery Engine service account to call your Cloud Run service:
```bash
gcloud run services add-iam-policy-binding menageai \
  --member="serviceAccount:service-YOUR_PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role="roles/run.servicesInvoker" \
  --region=us-west1
```

---

## 4. Environment Variables

Configure the following environment variables. In Cloud Run, these are set during deployment:

| Variable | Description | Example Value |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | Enables enterprise Vertex AI endpoint routing | `true` |
| `GOOGLE_CLOUD_PROJECT` | Active Google Cloud Project ID | `YOUR_PROJECT_ID` |
| `GOOGLE_CLOUD_LOCATION` | Active Vertex AI location | `global` |
| `USE_MCP_DEALS` | Enable deals extraction and grounding validation | `true` |
| `TWILIO_ACCOUNT_SID` | Your Twilio Account SID (for WhatsApp integration) | `AC...` |
| `TWILIO_AUTH_TOKEN` | Your Twilio Auth Token (for WhatsApp integration) | `d0...` |
| `TWILIO_NUMBER` | Your Twilio Outbound WhatsApp Number | `whatsapp:Your Twilio Outbound WhatsApp Number` |
| `FAMILY_WHATSAPP_NUMBERS` | Comma-separated list of approved sender numbers | `whatsapp:approved sender number1`,`whatsapp:approved sender number2` |

---

## 5. Build and Deploy Commands

### Method A: Using Manual `gcloud` Commands (Recommended)

1. **Create the Artifact Registry Docker repository**:
   ```bash
   gcloud artifacts repositories create YOUR_PROJECT_REPO \
       --repository-format=docker \
       --location=us-west1 \
       --description="Docker repository"
   ```

2. **Submit the build to Cloud Build**:
   ```bash
   gcloud builds submit \
       --tag us-west1-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_PROJECT_REPO/menageai:latest
   ```

3. **Deploy to Cloud Run with public access enabled**:
   ```bash
   gcloud run deploy menageai \
       --image us-west1-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_PROJECT_REPO/menageai:latest \
       --region us-west1 \
       --allow-unauthenticated \
       --update-env-vars GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,USE_MCP_DEALS=true,TWILIO_NUMBER=whatsapp:YOUR_TWILIO_OUTBOUND_WHATSAPP_NUMBER,FAMILY_WHATSAPP_NUMBERS=whatsapp:SENDER_NUMBER1,whatsapp:SENDER_NUMBER2,... \
       --update-secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest
   ```

---

### Method B: Using `agents-cli`

If using ADK's built-in deploy tool:
```bash
agents-cli deploy \
  --project YOUR_PROJECT_ID \
  --region us-west1 \
  --service-name menageai \
  --no-confirm-project \
  --secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN \
  --update-env-vars FAMILY_WHATSAPP_NUMBERS=whatsapp:SENDER_NUMBER1,whatsapp:SENDER_NUMBER2,...,TWILIO_NUMBER=whatsapp:YOUR_TWILIO_OUTBOUND_WHATSAPP_NUMBER,...
```

---

## 6. How to Reproduce the Deployment

Follow these sequential steps to reproduce this exact deployment in a clean project:

1. **Clone/Checkout the Repository** in your workspace.
2. **Log in** to your target GCP Project and enable the required APIs (listed in Section 2).
3. Ensure the Twilio Secrets are stored in GCP Secret Manager under the names `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` (if using Twilio integration).
4. Run the **Build** command to package and submit the container to Artifact Registry.
5. Run the **Deploy** command. Once completed, note the generated service URL.
6. Run the IAM command in Section 3B to allow Gemini Enterprise to invoke your service.
7. Open the generated Service URL in an **incognito window** to test the web client.
8. Update your Twilio Console WhatsApp Webhook to point to `https://YOUR_SERVICE_URL/twilio/webhook` to test mobile WhatsApp messaging.
