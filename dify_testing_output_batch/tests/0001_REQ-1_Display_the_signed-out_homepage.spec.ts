import { test, expect } from '@playwright/test';

test.describe('REQ-1 Homepage and session display', () => {
  test('REQ-1: SCN-REQ-1-01 Display the signed-out homepage', async ({ page, context }) => {
    await context.clearCookies();

    const mutatingRequests: { method: string; url: string }[] = [];
    page.on('request', (request) => {
      const method = request.method().toUpperCase();
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
        mutatingRequests.push({ method, url: request.url() });
      }
    });

    await page.goto('/');

    const registerLink = page.getByRole('link', { name: 'Register', exact: true });
    const loginLink = page.getByRole('link', { name: 'Login', exact: true });

    await expect(registerLink).toBeVisible();
    await expect(registerLink).toHaveAttribute('href', '/register');
    await expect(loginLink).toBeVisible();
    await expect(loginLink).toHaveAttribute('href', '/login');

    await expect(page.locator('input[type="password"]')).toHaveCount(0);

    const origin = new URL(page.url()).origin;
    const sameOriginMutations = mutatingRequests.filter(({ url }) => {
      try {
        return new URL(url).origin === origin;
      } catch {
        return false;
      }
    });
    expect(sameOriginMutations, 'Homepage load must not issue same-origin mutating requests').toEqual([]);

    const cookies = await context.cookies();
    const authCookies = cookies.filter((cookie) => /session|auth|token|jwt|sid|user/i.test(cookie.name));
    expect(authCookies, 'Homepage must not create an authentication session cookie').toEqual([]);
  });

  test('REQ-1: SCN-REQ-1-01 Refresh keeps the visitor signed out', async ({ page, context }) => {
    await context.clearCookies();
    await page.goto('/');

    const registerLink = page.getByRole('link', { name: 'Register', exact: true });
    const loginLink = page.getByRole('link', { name: 'Login', exact: true });

    await expect(registerLink).toBeVisible();
    await expect(loginLink).toBeVisible();

    await page.reload();

    await expect(registerLink).toBeVisible();
    await expect(registerLink).toHaveAttribute('href', '/register');
    await expect(loginLink).toBeVisible();
    await expect(loginLink).toHaveAttribute('href', '/login');

    await expect(page.locator('input[type="password"]')).toHaveCount(0);

    const cookies = await context.cookies();
    const authCookies = cookies.filter((cookie) => /session|auth|token|jwt|sid|user/i.test(cookie.name));
    expect(authCookies, 'Refreshing signed-out homepage must not create an authentication session cookie').toEqual([]);
  });
});
