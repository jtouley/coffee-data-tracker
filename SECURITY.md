# Security policy

## Reporting a vulnerability

Please don't describe a vulnerability in a public issue or pull request.

Report it privately through GitHub: on the repo's **Security** tab, choose **Report a vulnerability**
(<https://github.com/jtouley/coffee-data-tracker/security/advisories/new>). Include what's affected, the steps to
reproduce it and the impact you expect.

If the **Report a vulnerability** button is missing, or you've had no reply in 14 days, open a public issue titled
"Security contact request", with no details, and the owner will set up a private channel.

You'll get an acknowledgement within 14 days. The report and the fix are discussed in the private advisory, and
the advisory is published once a fix is out.

## Scope

In scope:

- the code in this repo, including its GitHub Actions workflows;
- the public site, [registry.coffee](https://registry.coffee);
- the app, [app.registry.coffee](https://app.registry.coffee).

Out of scope:

- roasters' own sites and stores;
- the accuracy of the catalog data. Report a wrong coffee or attribute on the
  [issue tracker](https://github.com/jtouley/coffee-data-tracker/issues).

## Supported versions

Only the latest `main` is supported. Code fixes land on `main`, and the app deploys from `main`, so a fix is live
once it's deployed. It's also included in the next tagged code release.
