# Use an official Python 3.9-slim image
FROM python:3.9-slim

# Set the working directory to /app
WORKDIR /app

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose port 8000
EXPOSE 8000

# Run the FastAPI backend server using uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
