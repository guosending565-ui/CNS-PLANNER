"""Local HTTP transport; import concrete server/router modules explicitly.

Keeping package import side-effect free allows security and DTO helpers to be
used in non-QGIS unit tests.
"""
