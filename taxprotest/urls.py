"""
URL configuration for taxprotest project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from .views import index, export_csv, similar_properties  # Import your views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", index, name="index"),  # Root URL
    path("export/", export_csv, name="export_csv"),  # CSV export
    path("similar/<str:account_number>/", similar_properties, name="similar_properties"),  # Similar properties
]
