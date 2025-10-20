# TaxProtest-Django# TaxProtest-Django



A Django web application for property tax analysis and comparison, using Harris County Appraisal District (HCAD) public data. Features include property search, similarity matching, automated data imports, and comprehensive property details including building specifications and extra features (pools, garages, etc.).## Standard Development Workflow (Docker Compose)



## Features### 1. Environment Setup



- 🔍 **Property Search** - Search by owner name, address, street name, or zip codeCopy `.env.example` to `.env` and set a strong `DJANGO_SECRET_KEY` (generate one with:

- 📊 **Similar Properties** - Find comparable properties using location, size, age, and features```bash

- 🏠 **Building Details** - View square footage, year built, bedrooms, bathrooms, and morepython3 -c "import secrets; print(secrets.token_urlsafe(64))"

- 🏊 **Extra Features** - See pools, garages, patios, and other amenities```

- 📍 **GIS Mapping** - Location-based search with latitude/longitude coordinates)

- 📥 **CSV Export** - Export search results to spreadsheets

- ⏰ **Automated Imports** - Monthly scheduled updates from HCAD data### 2. Start All Services (Recommended)

- 📈 **Pagination** - Browse large result sets efficiently

Run the following to start Postgres, Redis, and Django together:

## Quick Start```bash

docker compose up --build

### Prerequisites```



- Docker and Docker ComposeThis will automatically run migrations and start the Django server at `http://localhost:8000`. The `.env` file will be loaded by default.

- 4GB+ RAM recommended

- 10GB+ free disk space for HCAD data### 3. (Optional) Run Celery Worker

To run a Celery worker, uncomment the `worker` service in `docker-compose.yml` and run:

### 1. Clone and Configure```bash

docker compose up worker

```bash```

git clone https://github.com/PorkChopExpress86/TaxProtest-Django.git

cd TaxProtest-Django### 4. Manual Python Workflow (Advanced)

You can still use a virtualenv and run commands manually, but Docker Compose is the default and recommended method for all environments.

# Create environment file

cp .env.example .env---

## Security Note

# Generate a secret key

python3 -c "import secrets; print(secrets.token_urlsafe(64))"The framework secret key is no longer committed to the repository. Ensure you rotate any previously deployed key since it was present in history.



# Add the secret key to .envThe project uses a top-level `templates/` directory and a minimal `data` app with a sample Celery task at `data/tasks.py`.
echo "DJANGO_SECRET_KEY=<your-generated-key>" >> .env
```

### 2. Start the Application

```bash
# Start all services (Django, PostgreSQL, Redis, Celery)
docker compose up --build
```

The application will be available at **http://localhost:8000**

Services running:
- **web** - Django application (port 8000)
- **db** - PostgreSQL database (port 5432)
- **redis** - Redis message broker (port 6379)
- **worker** - Celery background worker
- **beat** - Celery scheduler for automated tasks

### 3. Import Data

**Initial Property Data Import:**
```bash
# Import HCAD property records (required - ~1.6M properties)
docker compose exec web python manage.py import_hcad_data
```

**GIS Location Data (recommended):**
```bash
# Import latitude/longitude coordinates (~30-45 min)
docker compose exec web python manage.py load_gis_data
```

**Building Details & Features (recommended):**
```bash
# Import building specs, bedrooms, bathrooms, features (~60-90 min)
docker compose exec web python manage.py import_building_data
```

See [DATABASE.md](DATABASE.md) for detailed import documentation.

## Usage

### Search Properties

1. Go to http://localhost:8000
2. Enter search criteria (owner name, address, zip, etc.)
3. View results with property details, building specs, and features
4. Click "Similar" to find comparable properties

### Find Similar Properties

The similarity algorithm considers:
- **Distance** (within 5 miles by default)
- **Size** (±30% building area)
- **Age** (±10 years)
- **Features** (matching pools, garages, etc.)
- **Bedrooms/Bathrooms** (exact or close matches)

Results are scored 0-100% similarity and ranked.

### Export Data

Click "Export All Results to CSV" on any search to download a spreadsheet with:
- Account numbers, owner names, addresses
- Assessed values and square footage
- Bedrooms, bathrooms, and features
- Price per square foot

## Automated Data Updates

### Monthly Building Data Import
- **Schedule:** 2nd Tuesday of each month at 2:00 AM Central
- **What:** Building details, features, bedrooms, bathrooms
- **Duration:** 60-90 minutes

### Annual GIS Update
- **Schedule:** January 15th at 3:00 AM Central
- **What:** Property coordinates (latitude/longitude)
- **Duration:** 30-45 minutes

### Manual Triggers

```bash
# Trigger building data import
docker compose exec web python manage.py import_building_data

# Trigger GIS update
docker compose exec web python manage.py load_gis_data

# Link orphaned building/feature records
docker compose exec web python manage.py link_orphaned_records
```

## Development

### Project Structure

```
TaxProtest-Django/
├── taxprotest/          # Django project settings
│   ├── settings.py      # Configuration
│   ├── urls.py          # URL routing
│   ├── views.py         # Main views
│   ├── celery.py        # Celery configuration
│   └── wsgi.py          # WSGI application
├── data/                # Data app
│   ├── models.py        # PropertyRecord, BuildingDetail, ExtraFeature
│   ├── etl.py           # Data import functions
│   ├── tasks.py         # Celery background tasks
│   ├── similarity.py    # Property comparison algorithm
│   └── admin.py         # Django admin configuration
├── templates/           # HTML templates
│   ├── base.html        # Base template
│   ├── index.html       # Search page
│   └── similar_properties.html
├── downloads/           # HCAD data files (auto-created)
└── docker-compose.yml   # Docker services
```

### Key Technologies

- **Django 5.x** - Web framework
- **PostgreSQL** - Database
- **Redis** - Message broker
- **Celery** - Background task processing
- **Celery Beat** - Task scheduling
- **Bootstrap 5** - UI framework
- **GeoPandas** - GIS data processing

### Common Commands

```bash
# Access Django shell
docker compose exec web python manage.py shell

# Create database migrations
docker compose exec web python manage.py makemigrations

# Apply migrations
docker compose exec web python manage.py migrate

# Create superuser for admin
docker compose exec web python manage.py createsuperuser

# View logs
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f beat

# Stop services
docker compose down

# Stop and remove volumes
docker compose down -v
```

### Django Admin

Access the admin panel at **http://localhost:8000/admin/** (create superuser first)

Features:
- View/filter PropertyRecords
- View/filter BuildingDetails with import metadata
- View/filter ExtraFeatures
- Manually trigger GIS or building data imports
- View download history

## Documentation

- **[SETUP.md](SETUP.md)** - Detailed installation and configuration
- **[DATABASE.md](DATABASE.md)** - Data imports, ETL processes, and database management
- **[GIS.md](GIS.md)** - GIS data handling and location features

## Data Source

All property data comes from **Harris County Appraisal District (HCAD)**:
- https://hcad.org/
- https://download.hcad.org/data/

Data files used:
- **Real_acct_owner.txt** - Property records (owners, addresses, values)
- **Real_building_land.zip** - Building details, features, fixtures
- **Parcels.zip** - GIS shapefiles with coordinates

## Troubleshooting

### Services won't start
```bash
# Check Docker is running
docker ps

# Check logs for errors
docker compose logs

# Rebuild containers
docker compose up --build
```

### Database connection errors
```bash
# Check PostgreSQL is running
docker compose ps db

# Reset database
docker compose down -v
docker compose up --build
```

### Import fails
```bash
# Check worker logs
docker compose logs worker

# Check disk space
df -h

# Re-run import
docker compose exec web python manage.py import_building_data
```

### No search results
- Ensure property data has been imported
- Check search criteria (be less specific)
- Verify data in Django admin

## Performance

**Database Size:**
- PropertyRecords: ~1.6M records (~2GB)
- BuildingDetails: ~1.3M records (~1GB)
- ExtraFeatures: 0-5M records (~500MB-2GB)
- Total: ~5-10GB with all data

**Search Performance:**
- Typical search: <500ms
- Paginated results: 200 properties per page
- Similarity search: 1-3 seconds

**Import Duration:**
- Property records: 15-30 minutes
- GIS data: 30-45 minutes
- Building data: 60-90 minutes

## Contributing

This is a personal project for property tax analysis. Feel free to fork and adapt for your own use.

## License

See [LICENSE](LICENSE) file for details.

## Security Note

- Never commit `.env` files
- Rotate Django secret keys if exposed
- Keep dependencies updated
- Use HTTPS in production
- Configure ALLOWED_HOSTS properly

---

**Made with ❤️ for property tax analysis in Harris County, Texas**
