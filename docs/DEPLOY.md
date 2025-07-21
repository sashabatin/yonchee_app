# Deploying Yonchee Bot to Azure Container Apps

This guide describes how to deploy Yonchee Bot as an Azure Container App using a private Azure Container Registry (ACR), securely managing secrets, mapping environment variables, and automating deployment with GitHub Actions.

---

## Prerequisites

- Azure Subscription ([Get one for free](https://azure.microsoft.com/free/))
- [Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli) installed and logged in
- Docker installed (locally or use GitHub Actions)
- Source code and Dockerfile ready
- Azure Container Registry (ACR) created
- Azure Resource Group created

---

## 1. Build and Push Docker Image to ACR

**A. Build the Docker image:**
```sh
docker build -t <acr-name>.azurecr.io/<image-name>:<tag> .
# Example: docker build -t myacr.azurecr.io/yonchee-bot:dev .
```

**B. Login to ACR and push the image:**
```sh
az acr login --name <acr-name>
# Example: az acr login --name myacr

docker push <acr-name>.azurecr.io/<image-name>:<tag>
# Example: docker push myacr.azurecr.io/yonchee-bot:dev
```

*Alternatively, use [GitHub Actions](https://docs.github.com/en/actions/publishing-packages/publishing-docker-images) to automate builds and pushes.*

---

## 2. Create Azure Container Apps Environment

```sh
az containerapp env create \
  --name <container-app-env-name> \
  --resource-group <resource-group> \
  --location <azure-region>
# Example: az containerapp env create --name my-env --resource-group my-rg --location westeurope
```
- See [Azure Container Apps Environment Docs](https://learn.microsoft.com/en-us/azure/container-apps/environment)

---

## 3. Create the Container App with Registry Credentials

**Get ACR credentials:**
```sh
az acr credential show --name <acr-name> --resource-group <resource-group>
# Example: az acr credential show --name myacr --resource-group my-rg
```

**Create the app:**
```sh
az containerapp create \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --environment <container-app-env-name> \
  --image <acr-name>.azurecr.io/<image-name>:<tag> \
  --registry-server <acr-name>.azurecr.io \
  --registry-username <ACR_USERNAME> \
  --registry-password <ACR_PASSWORD> \
  --cpu 0.25 --memory 0.5Gi \
  --min-replicas 0 --max-replicas 2
# Example: az containerapp create --name yonchee-bot-dev --resource-group my-rg --environment my-env --image myacr.azurecr.io/yonchee-bot:dev ...
```
- See [Container Apps CLI Reference](https://learn.microsoft.com/en-us/cli/azure/containerapp)

---

## 4. Set Secrets for API Keys and Tokens

```sh
az containerapp secret set \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --secrets \
    azure-form-recognizer-endpoint=<your-endpoint> \
    azure-form-recognizer-key=<your-key> \
    azure-speech-api-key=<your-speech-key> \
    azure-region=<your-region> \
    telegram-api-token=<your-telegram-token>
# Example: az containerapp secret set --name yonchee-bot-dev --resource-group my-rg --secrets azure-form-recognizer-endpoint=...
```
- [Azure Container Apps: Secure app configuration and secrets](https://learn.microsoft.com/en-us/azure/container-apps/secrets)

---

## 5. Map Secrets to Environment Variables

```sh
az containerapp update \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --set-env-vars \
    AZURE_FORM_RECOGNIZER_ENDPOINT=secretref:azure-form-recognizer-endpoint \
    AZURE_FORM_RECOGNIZER_KEY=secretref:azure-form-recognizer-key \
    AZURE_SPEECH_API_KEY=secretref:azure-speech-api-key \
    AZURE_REGION=secretref:azure-region \
    TELEGRAM_API_TOKEN=secretref:telegram-api-token
# Example: az containerapp update --name yonchee-bot-dev --resource-group my-rg --set-env-vars AZURE_FORM_RECOGNIZER_ENDPOINT=secretref:...
```
- [Environment variables in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/environment-variables)

---

## 6. Update Image on New Deployments

If you push a new image to ACR, update the Container App:

```sh
az containerapp update \
  --name <container-app-name> \
  --resource-group <resource-group> \
  --image <acr-name>.azurecr.io/<image-name>:<tag>
# Example: az containerapp update --name yonchee-bot-dev --resource-group my-rg --image myacr.azurecr.io/yonchee-bot:dev
```

---

## 7. Check Logs and Verify the App is Running

- In the Azure Portal, go to **Container Apps → <container-app-name> → Logs**.
- Test your bot to ensure it is working as expected.

---

## 8. CI/CD Automation with GitHub Actions (Recommended)

You can automate building, pushing, and deploying your app using GitHub Actions.

### **Required GitHub Secrets:**
- `ACR_LOGIN_SERVER`
- `ACR_USERNAME`
- `ACR_PASSWORD`
- `AZURE_CREDENTIALS` (Service Principal JSON)
- `AZURE_RESOURCE_GROUP` (your resource group name)

### **Sample Workflow:**

```yaml
name: Build, Push, and Deploy Docker image to ACR

on:
  push:
    branches: [ main, dev ]

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.vars.outputs.TAG }}
    steps:
      - uses: actions/checkout@v3

      - name: Set image tag
        id: vars
        run: |
          if [[ "${GITHUB_REF##*/}" == "main" ]]; then
            echo "TAG=latest" >> $GITHUB_OUTPUT
          else
            echo "TAG=${GITHUB_REF##*/}" >> $GITHUB_OUTPUT
          fi

      - name: Log in to Azure Container Registry
        uses: docker/login-action@v2
        with:
          registry: ${{ secrets.ACR_LOGIN_SERVER }}
          username: ${{ secrets.ACR_USERNAME }}
          password: ${{ secrets.ACR_PASSWORD }}

      - name: Build and push Docker image
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: ${{ secrets.ACR_LOGIN_SERVER }}/yonchee-bot:${{ steps.vars.outputs.TAG }}

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    environment:
      name: dev  # or "production" for main branch
    steps:
      - name: Azure Login
        uses: azure/login@v1
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}

      - name: Debug - List container apps in resource group
        run: |
          echo "Listing container apps in resource group: ${{ secrets.AZURE_RESOURCE_GROUP }}"
          az containerapp list --resource-group ${{ secrets.AZURE_RESOURCE_GROUP }} --output table

      - name: Deploy to Azure Container App
        run: |
          az containerapp update \
            --name yonchee-bot-dev \
            --resource-group ${{ secrets.AZURE_RESOURCE_GROUP }} \
            --image ${{ secrets.ACR_LOGIN_SERVER }}/yonchee-bot:${{ needs.build-and-push.outputs.tag }}
```

- Adjust `--name yonchee-bot-dev` for your dev or main app as needed.
- For production, use a different environment and app name.

---

## Best Practices

- Use [Azure Key Vault](https://learn.microsoft.com/en-us/azure/key-vault/general/basic-concepts) for advanced secret management.
- For production, consider using [Managed Identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity-authentication?tabs=azure-cli%2Cazure-cli-2) for ACR access.
- Automate deployment with [GitHub Actions](https://docs.github.com/en/actions) or [Terraform](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app) as you grow.
- Use separate Container Apps and secrets for dev and main/production environments.

---

## Useful Links

- [Azure Container Apps Documentation](https://learn.microsoft.com/en-us/azure/container-apps/)
- [Azure CLI Reference](https://learn.microsoft.com/en-us/cli/azure/containerapp)
- [Azure Container Registry Documentation](https://learn.microsoft.com/en-us/azure/container-registry/)
- [GitHub Actions for Azure](https://github.com/Azure/actions)
- [Deploy to Azure Container Apps with GitHub Actions](https://learn.microsoft.com/en-us/azure/container-apps/github-actions)

---

## Deploying Multiple Versions (e.g., dev and main)

Repeat the above steps for each version, using different names/tags:

- **Dev version:**  
  - `<container-app-name>`: `yonchee-bot-dev`
  - `<tag>`: `dev`
- **Main/Production version:**  
  - `<container-app-name>`: `yonchee-bot-main` (or just `yonchee-bot`)
  - `<tag>`: `latest` or `main`

This allows you to run and test both environments independently.

---

## Troubleshooting

- If your GitHub Actions workflow cannot update the app, check:
  - The resource group and app name in your secrets and workflow.
  - The service principal permissions (Contributor on the resource group).
  - That your Docker image is pushed to ACR with the correct tag.
- Use the debug step in the workflow to list container apps in your resource group.

---

*For any issues, consult the official Azure and GitHub Actions documentation linked above.*