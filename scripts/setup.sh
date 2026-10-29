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

# Create .env, or fill any generated secret it is missing
python3 scripts/env_setup.py || exit 1

# Build and Start Containers
echo -e "\n${YELLOW}Deploying for Production? (y/N)${NC}"
read -t 10 -n 1 -r REPLY_PROD
echo
COMPOSE_FILE=""
if [[ $REPLY_PROD =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}Using Production Configuration (docker-compose.prod.yml)...${NC}"
    COMPOSE_FILE="-f docker-compose.prod.yml"
else
    echo -e "${GREEN}Using Development Configuration (docker-compose.yml)...${NC}"
    COMPOSE_FILE=""
fi

echo -e "\n${YELLOW}Building and starting Docker containers...${NC}"
docker compose $COMPOSE_FILE up -d --build

echo -e "\n${YELLOW}Waiting for database to be ready (10s)...${NC}"
sleep 10

# Run Migrations
echo -e "\n${YELLOW}Running database migrations...${NC}"
docker compose $COMPOSE_FILE exec web python manage.py migrate

# Create Superuser (interactive check)
echo -e "\n${YELLOW}Would you like to create a superuser? (y/n)${NC}"
read -t 10 -n 1 -r REPLY
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker compose $COMPOSE_FILE exec web python manage.py createsuperuser
fi

# Data Import
echo -e "\n${GREEN}===============================================${NC}"
echo -e "${GREEN}              DATA IMPORT PHASE                ${NC}"
echo -e "${GREEN}===============================================${NC}"
echo -e "${YELLOW}Data import is now automated on container startup.${NC}"
echo -e "${YELLOW}Check progress with: docker compose logs -f web${NC}"

# Property Records
# echo -e "\n${GREEN}[1/3] Importing Property Records...${NC}"
# docker compose $COMPOSE_FILE exec web python manage.py load_hcad_real_acct

# Building Data
# echo -e "\n${GREEN}[2/3] Importing Building Data (with progress details)...${NC}"
# docker compose $COMPOSE_FILE exec web python manage.py import_building_data

# GIS Data
# echo -e "\n${GREEN}[3/3] Importing GIS Data (with progress details)...${NC}"
# docker compose $COMPOSE_FILE exec web python manage.py load_gis_data

echo -e "\n${GREEN}===============================================${NC}"
echo -e "${GREEN}           SETUP COMPLETE! 🎉                  ${NC}"
echo -e "${GREEN}===============================================${NC}"
echo -e "Access the application at: http://localhost:8000"
