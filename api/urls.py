from django.urls import path
from . import views
 
urlpatterns = [
    # Main endpoints
    path('analyze/', views.analyze_listing, name='analyze_listing'),
    path('task-status/<uuid:task_id>/', views.task_status, name='task_status'),
    
    # History endpoints
    path('history/', views.analysis_history, name='analysis_history'),
    path('analysis/<uuid:task_id>/', views.get_analysis, name='get_analysis'),
    
    # Health check
    path('health/', views.health_check, name='health_check'),
    path("analysis/<uuid:task_id>/download-pdf/", views.download_analysis_pdf, name="download_analysis_pdf"),
]