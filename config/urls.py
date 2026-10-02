from django.urls import include, path
from django.views.decorators.csp import csp_override
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.planner.views import health

# Swagger UI is a third-party page that pulls scripts from a CDN and inlines its
# bootstrap code, so it is exempted from the deny-all CSP (an empty policy = no header).
swagger_ui = csp_override({})(SpectacularSwaggerView.as_view(url_name="schema"))

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="docs"), name="root"),
    path("api/v1/", include("apps.planner.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", swagger_ui, name="docs"),
    path("health/", health, name="health"),
]

handler404 = "apps.planner.views.not_found"
handler500 = "apps.planner.views.server_error"
