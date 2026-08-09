from django.urls import path

from . import views

urlpatterns = [
    path("", views.brazos_index, name="brazos_index"),
    path("protest/<str:prop_id>/", views.protest_analysis, name="brazos_protest_analysis"),
]
