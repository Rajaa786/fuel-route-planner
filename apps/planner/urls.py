from django.urls import path

from apps.planner import views

app_name = "planner"

urlpatterns = [
    path("route-plan/", views.RoutePlanView.as_view(), name="route-plan"),
    path("route-plan/map/", views.route_map, name="route-map"),
]
