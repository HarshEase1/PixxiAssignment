"""
Django models for Amazon Listing Analyzer

File: api/models.py
"""

from django.db import models
import uuid


class AnalysisTask(models.Model):
    """Track progress of ongoing analysis tasks"""
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asin = models.CharField(max_length=10, db_index=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.IntegerField(default=0)  # 0-100
    message = models.TextField(blank=True)
    
    error = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['asin', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.asin} - {self.status} ({self.progress}%)"


class AnalysisResult(models.Model):
    """Store completed analysis results"""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.OneToOneField(AnalysisTask, on_delete=models.CASCADE, related_name='result')
    asin = models.CharField(max_length=10, db_index=True)
    
    # Your Product Data
    your_product_data = models.JSONField()  # Stores full product dict
    
    # Competitors Data
    competitors_data = models.JSONField()  # List of competitor dicts
    
    # AI Analysis
    analysis_text = models.TextField()  # Markdown/text analysis from DeepSeek
    
    # HTML Previews
    your_product_html = models.TextField()
    competitor_1_html = models.TextField(blank=True)
    competitor_2_html = models.TextField(blank=True)
    competitor_3_html = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['asin', '-created_at']),
        ]
    
    def __str__(self):
        return f"Analysis: {self.asin}"
    
    @property
    def competitor_htmls(self):
        """Return list of competitor HTMLs"""
        htmls = []
        if self.competitor_1_html:
            htmls.append(self.competitor_1_html)
        if self.competitor_2_html:
            htmls.append(self.competitor_2_html)
        if self.competitor_3_html:
            htmls.append(self.competitor_3_html)
        return htmls