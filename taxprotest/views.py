# home/views.py

from django.shortcuts import render
from data.models import PropertyRecord



def index(request):
    results = []
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        address = request.POST.get('address', '').strip()
        street_name = request.POST.get('street_name', '').strip()
        zip_code = request.POST.get('zip_code', '').strip()

        # Build queryset filters
        filters = {}
        if address:
            filters['address__icontains'] = address
        if street_name:
            filters['address__icontains'] = street_name
        if zip_code:
            filters['zipcode__icontains'] = zip_code

        # Only one field required, so OR logic for name (if owner table loaded in future)
        # For now, just address/street/zip
        if filters:
            qs = PropertyRecord.objects.filter(**filters)
        else:
            qs = PropertyRecord.objects.all()

        # Ordering: zipcode, then street number, then street name
        qs = qs.order_by('zipcode', 'street_number', 'street_name')[:200]

        # Format for template
        formatted = []
        for prop in qs:
            assessed = prop.assessed_value or prop.value
            bldg_area = prop.building_area or 0
            ppsf = None
            if assessed and bldg_area and bldg_area > 0:
                try:
                    ppsf = float(assessed) / float(bldg_area)
                except Exception:
                    ppsf = None
            formatted.append({
                'account_number': prop.account_number,
                'owner_name': prop.owner_name,
                'address': prop.street_number,
                'street_name': prop.street_name,
                'zip_code': prop.zipcode,
                'assessed_value': assessed,
                'building_area': prop.building_area,
                'land_area': prop.land_area,
                'ppsf': ppsf,
            })
        results = formatted

    return render(request, "index.html", {'results': results})


## Removed mock results function; now using real data
