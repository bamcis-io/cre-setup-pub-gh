/**
 * External DNS: {@link ValidatedCertificate}, an ACM certificate DNS-validated automatically in a
 * hosted zone in this account or another one; {@link CreDnsStack}, the `<project>-dns` stack
 * (deployed into the zone's account by the project type's external-DNS phase) that holds the
 * records a demo publishes; and {@link PrivateApiCustomDomain} + {@link PrivateApiDnsRecords}, custom
 * domain names for a private REST API.
 */
export * from './validated-certificate.js';
export * from './private-api-domain.js';
