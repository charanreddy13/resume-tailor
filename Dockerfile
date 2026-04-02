FROM python:3.11-slim

# Install pdflatex and LaTeX packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    texlive-fonts-extra \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY server_final.py .
COPY index.html .
COPY charan_reddy.tex .

# Create necessary directories
RUN mkdir -p outputs logs credentials

# Render injects PORT at runtime
ENV PORT=8080

CMD ["python", "server_final.py"]
