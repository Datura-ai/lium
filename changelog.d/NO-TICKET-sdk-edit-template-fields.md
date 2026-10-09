### Fixed
- `Lium.edit` builds the template it sends back from `GET /templates/{id}` instead of the pod's template summary, so editing a pod no longer resets its template's registry credential, Docker-in-Docker and volume-encryption flags and health check, and a template with NULL volumes, environment or entrypoint no longer makes the edit fail with 422.
