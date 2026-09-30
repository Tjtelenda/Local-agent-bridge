# Privacy Policy — Local Agent Bridge

*Last updated: September 19, 2026*

**Task inputs and results pass through relay memory and may contain personal information.**

## Who we are

Local Agent Bridge is built and operated by Trent Telenda, an independent developer.
Contact: tjtelendallc@gmail.com

## How the service works

You run a small open-source bridge app on your own machine. It connects outward to
our relay, which passes task requests and responses between an API client and your bridge.
That is all the relay does.

## What is held in memory

- Task results are held in relay memory for five minutes by default, then
  swept within another minute. The application does not write them to disk.
  The bridge caches outcomes locally in process memory for replay protection.
- We keep no logs of message contents, no analytics, no tracking, and no cookies
  on the website.
- We have no user accounts. The only identifier is a random pairing token that
  links your bridge to an API client. It contains no personal information,
  and remains valid in relay memory until relay restart. Deleting a local
  token does not revoke relay credentials.

## What we never do

Task inputs and adapter results reach the relay and may contain sensitive data.
Local adapter configuration and credentials are not registered with the relay.
The local approval console displays task inputs for review. Meta/Muse delivery
is currently a logging stub; expose only actions appropriate for relay access.

## Changes

If this policy ever changes, the updated version will be posted here with a new
date. Continued use of the service means you accept the changes.
