# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install Node.js (required by the backend to run PptxGenJS scripts)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy dependency files from the root context
# Note: This assumes build context is the project root
COPY requirements.txt .
COPY package.json .
COPY package-lock.json* .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Node dependencies (primarily pptxgenjs)
RUN npm install

# Copy the Backend source code
COPY Backend/ .

# Ensure the outputs directory exists and is writable
RUN mkdir -p outputs/icons && chmod -R 777 outputs

# Expose the API port
EXPOSE 4001

# Environment variables for AWS (to be overridden at runtime)
ENV AWS_REGION=us-east-1
ENV BEDROCK_MODEL_ID=us.anthropic.claude-3-7-sonnet-20250219-v1:0
ENV OUTPUT_DIR=./outputs

# Command to run the application
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "4001"]