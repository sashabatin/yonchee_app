# Yonchee Bot

Yonchee Bot is a Telegram bot designed to assist vision-impaired individuals by converting text from images or PDFs into audio format.

## Features
- Upload photos and PDFs for text extraction.
- Convert extracted text to speech.
- Receive audio files directly in Telegram.

## Setup Instructions

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd yonchee-bot

## How to Add ffmpeg to Windows PATH

1. **Extract ffmpeg**  
   After downloading, extract the ffmpeg archive to a folder (e.g., `C:\ffmpeg`).

2. **Locate the `bin` Directory**  
   Open the extracted folder and navigate to the `bin` directory.  
   Example path:  
   ```
   C:\ffmpeg\ffmpeg-<version>-essentials_build\bin
   ```
   (e.g., `C:\ffmpeg\ffmpeg-6.1.1-essentials_build\bin`)

3. **Copy the Full Path**  
   Copy the full path to the `bin` folder.

4. **Add the Path to Windows Environment Variables**
   - Press <kbd>Win</kbd> + <kbd>S</kbd> and type `environment variables`, then select **Edit the system environment variables**.
   - In the **System Properties** window, click **Environment Variables...**.
   - In the **System variables** section, scroll down and select the `Path` variable, then click **Edit...**.
   - Click **New** and paste the path to the `bin` folder you copied earlier.
   - Click **OK** on all windows to save and close.

5. **Verify Installation**
   - Open a new Command Prompt window and run:
     ```
     ffmpeg -version
     ```
   - You should see version information for ffmpeg if everything is set up correctly.

---

**Note:**  
Adding ffmpeg to your PATH allows your bot to use ffmpeg from anywhere on your system.