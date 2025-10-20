# home/views.py

from django.shortcuts import render
from django.core.paginator import Paginator
from django.utils.http import urlencode
from django.http import HttpResponse
import csv
from data.models import PropertyRecord
from data.similarity import find_similar_properties, format_feature_list



def index(request):
    results = []
    page_obj = None

    query_source = request.GET if request.method == 'GET' else request.POST

    first_name = query_source.get('first_name', '').strip()
    last_name = query_source.get('last_name', '').strip()
    address = query_source.get('address', '').strip()
    street_name = query_source.get('street_name', '').strip()
    zip_code = query_source.get('zip_code', '').strip()
    page_number = query_source.get('page', '1')

    filters_applied = any([first_name, last_name, address, street_name, zip_code])

    if filters_applied:
        qs = PropertyRecord.objects.all()

        if address:
            qs = qs.filter(address__icontains=address)
        if street_name:
            qs = qs.filter(street_name__icontains=street_name)
        if zip_code:
            qs = qs.filter(zipcode__icontains=zip_code)
        if last_name:
            qs = qs.filter(owner_name__icontains=last_name)
        if first_name:
            qs = qs.filter(owner_name__icontains=first_name)

        qs = qs.order_by('zipcode', 'street_number', 'street_name')

        paginator = Paginator(qs, 200)
        page_obj = paginator.get_page(page_number)

        formatted = []
        for prop in page_obj.object_list:
            assessed = prop.assessed_value or prop.value
            bldg_area = prop.building_area or 0
            ppsf = None
            if assessed and bldg_area and bldg_area > 0:
                try:
                    ppsf = float(assessed) / float(bldg_area)
                except Exception:
                    ppsf = None
            
            # Get building details (bedrooms, bathrooms)
            building = prop.buildings.filter(is_active=True).first()
            bedrooms = building.bedrooms if building else None
            bathrooms = building.bathrooms if building else None
            
            # Get extra features (pool, garage, etc.)
            features = list(prop.extra_features.filter(is_active=True))
            features_text = format_feature_list(features, max_features=5) if features else None
            
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
                'bedrooms': bedrooms,
                'bathrooms': bathrooms,
                'features': features_text,
            })
        results = formatted

    query_params = request.GET.copy()
    query_params.pop('page', None)
    base_query = query_params.urlencode()

    form_values = {
        'first_name': first_name,
        'last_name': last_name,
        'address': address,
        'street_name': street_name,
        'zip_code': zip_code,
    }

    context = {
        'results': results,
        'page_obj': page_obj,
        'base_query': base_query,
        'form_values': form_values,
        'filters_applied': filters_applied,
    }

    return render(request, "index.html", context)


def export_csv(request):
    """Export all search results to CSV."""
    first_name = request.GET.get('first_name', '').strip()
    last_name = request.GET.get('last_name', '').strip()
    address = request.GET.get('address', '').strip()
    street_name = request.GET.get('street_name', '').strip()
    zip_code = request.GET.get('zip_code', '').strip()

    filters_applied = any([first_name, last_name, address, street_name, zip_code])

    if not filters_applied:
        # Return empty CSV if no filters
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="property_search.csv"'
        writer = csv.writer(response)
        writer.writerow(['No search criteria provided'])
        return response

    qs = PropertyRecord.objects.all()

    if address:
        qs = qs.filter(address__icontains=address)
    if street_name:
        qs = qs.filter(street_name__icontains=street_name)
    if zip_code:
        qs = qs.filter(zipcode__icontains=zip_code)
    if last_name:
        qs = qs.filter(owner_name__icontains=last_name)
    if first_name:
        qs = qs.filter(owner_name__icontains=first_name)

    qs = qs.order_by('zipcode', 'street_number', 'street_name')

    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="property_search.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Account Number',
        'Owner Name',
        'Street Number',
        'Street Name',
        'Zip Code',
        'Assessed Value',
        'Building Area (sqft)',
        'Bedrooms',
        'Bathrooms',
        'Features',
        'Price per sqft'
    ])

    for prop in qs:
        assessed = prop.assessed_value or prop.value
        bldg_area = prop.building_area or 0
        ppsf = None
        if assessed and bldg_area and bldg_area > 0:
            try:
                ppsf = float(assessed) / float(bldg_area)
            except Exception:
                ppsf = None
        
        # Get building details
        building = prop.buildings.filter(is_active=True).first()
        bedrooms = building.bedrooms if building else ''
        bathrooms = f'{float(building.bathrooms):.1f}' if building and building.bathrooms else ''
        
        # Get extra features
        features = list(prop.extra_features.filter(is_active=True))
        features_text = format_feature_list(features, max_features=10) if features else ''

        writer.writerow([
            prop.account_number,
            prop.owner_name,
            prop.street_number,
            prop.street_name,
            prop.zipcode,
            assessed if assessed else '',
            bldg_area if bldg_area else '',
            bedrooms,
            bathrooms,
            features_text,
            f'{ppsf:.2f}' if ppsf else ''
        ])

    return response


def similar_properties(request, account_number):
    """Find and display properties similar to the given account."""
    try:
        target_property = PropertyRecord.objects.get(account_number=account_number)
    except PropertyRecord.DoesNotExist:
        return render(request, 'similar_properties.html', {
            'error': 'Property not found',
            'account_number': account_number
        })
    
    # Get target building and features for display
    target_building = target_property.buildings.first()
    target_features = list(target_property.extra_features.all())
    
    # Check if property has required data
    if not target_property.latitude or not target_property.longitude:
        return render(request, 'similar_properties.html', {
            'error': 'This property does not have location data required for similarity search.',
            'target_property': target_property,
            'target_building': target_building,
        })
    
    # Get search parameters
    max_distance = float(request.GET.get('max_distance', '5'))
    max_results = int(request.GET.get('max_results', '20'))
    min_score = float(request.GET.get('min_score', '30'))
    
    # Find similar properties
    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=max_distance,
        max_results=max_results,
        min_score=min_score
    )
    
    # Format results for template
    formatted_results = []
    for result in similar:
        prop = result['property']
        building = result['building']
        features = result['features']
        
        assessed = prop.assessed_value or prop.value
        bldg_area = building.heat_area if building else (prop.building_area or 0)
        ppsf = None
        if assessed and bldg_area and bldg_area > 0:
            try:
                ppsf = float(assessed) / float(bldg_area)
            except Exception:
                ppsf = None
        
        formatted_results.append({
            'account_number': prop.account_number,
            'owner_name': prop.owner_name,
            'address': prop.street_number,
            'street_name': prop.street_name,
            'zip_code': prop.zipcode,
            'assessed_value': assessed,
            'building_area': bldg_area,
            'land_area': prop.land_area,
            'ppsf': ppsf,
            'distance': result['distance'],
            'similarity_score': result['similarity_score'],
            'year_built': building.year_built if building else None,
            'bedrooms': building.bedrooms if building else None,
            'bathrooms': building.bathrooms if building else None,
            'features': format_feature_list(features, max_features=5),
        })
    
    context = {
        'target_property': target_property,
        'target_building': target_building,
        'target_features': format_feature_list(target_features),
        'target_year_built': target_building.year_built if target_building else None,
        'target_bedrooms': target_building.bedrooms if target_building else None,
        'target_bathrooms': target_building.bathrooms if target_building else None,
        'target_area': target_building.heat_area if target_building else prop.building_area,
        'results': formatted_results,
        'max_distance': max_distance,
        'max_results': max_results,
        'min_score': min_score,
    }
    
    return render(request, 'similar_properties.html', context)


## Removed mock results function; now using real data
