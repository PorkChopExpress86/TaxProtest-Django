#!/bin/bash
# setup.sh - Automated setup script for TaxProtest-Django

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}===============================================${NC}"
echo -e "${GREEN}   TaxProtest-Django - Automated Setup Loop    ${NC}"
echo -e "${GREEN}===============================================${NC}"

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed.${NC}"
    exit 1
fi

# Check for .env file
if [ ! -f .env ]; then
    echo -e "${YELLOW}Creating .env file from .env.example...${NC}"
    cp .env.example .env
    # Generate secret key and update .env
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
    
    # Use sed to replace the placeholder or append if not found
    if grep -q "DJANGO_SECRET_KEY=" .env; then
         # simplistic replacement, might fail if key has special chars for sed, 
         # but token_urlsafe is usually safe-ish.
         # Ideally we'd ensure the .env.example has a specific placeholder.
         # For now, let's just warn user to check it.
         echo -e "${YELLOW}Please check .env and set DJANGO_SECRET_KEY and DB password.${NC}"
    else
         echo "DJANGO_SECRET_KEY=$SECRET_KEY" >> .env
    fi
    echo -e "${GREEN}.env created.${NC}"
else
    echo -e "${GREEN}.env file found.${NC}"
fi

# Build and Start Containers
echo -e "\n${YELLOW}Building and starting Docker containers...${NC}"
docker compose up -d --build

echo -e "\n${YELLOW}Waiting for database to be ready (10s)...${NC}"
sleep 10

# Run Migrations
echo -e "\n${YELLOW}Running database migrations...${NC}"
docker compose exec web python manage.py migrate

# Create Superuser (interactive check)
echo -e "\n${YELLOW}Would you like to create a superuser? (y/n)${NC}"
read -t 10 -n 1 -r REPLY
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker compose exec web python manage.py createsuperuser
fi

# Data Import
echo -e "\n${GREEN}===============================================${NC}"
echo -e "${GREEN}              DATA IMPORT PHASE                ${NC}"
echo -e "${GREEN}===============================================${NC}"
echo -e "${YELLOW}This process will download large files from HCAD.${NC}"
echo -e "${YELLOW}Progress will be shown below.${NC}"

# Property Records
echo -e "\n${GREEN}[1/3] Importing Property Records...${NC}"
docker compose exec web python manage.py load_hcad_real_acct

# Building Data
echo -e "\n${GREEN}[2/3] Importing Building Data (with progress details)...${NC}"
docker compose exec web python manage.py import_building_data

# GIS Data
echo -e "\n${GREEN}[3/3] Importing GIS Data (with progress details)...${NC}"
docker compose exec web python manage.py load_gis_data

echo -e "\n${GREEN}===============================================${NC}"
echo -e "${GREEN}           SETUP COMPLETE! 🎉                  ${NC}"
echo -e "${GREEN}===============================================${NC}"
echo -e "Access the application at: http://localhost:8000"
