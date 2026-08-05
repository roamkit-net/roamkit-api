"""Pagination for ops list endpoints (stable page size for V1 UI)."""

from rest_framework.pagination import PageNumberPagination


class OpsPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
