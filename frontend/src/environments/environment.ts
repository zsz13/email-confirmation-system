// Production build (`pnpm build`, used by the Docker image). An empty base URL means
// same-origin requests: NGINX serves the app and forwards /api/v1 to the FastAPI container.
export const environment = {
  apiBaseUrl: '',
};
