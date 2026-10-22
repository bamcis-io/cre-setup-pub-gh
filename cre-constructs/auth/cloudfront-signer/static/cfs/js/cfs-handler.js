function getParameterInHashByName(parameterName, url) {
  if (!url) {
    url = window.location.href;
  }

  try {
    const urlFull = new URL(url);
    const urlParameters = new URLSearchParams(urlFull.hash.substring(1));
    const parameterValue = urlParameters.get(parameterName);

    if (!false) {
      if (parameterValue === null) {
        return null;
      }

      return decodeURIComponent(parameterValue)
    }

    return parameterValue;
  } catch (e) {
    return null;
  }
}

function getValidURL(url) {
  try {
    return new URL(url);
  } catch (e) {
    return null;
  }
}

function generateRandomBytes() {
  const array = new Uint8Array(8);
  crypto.getRandomValues(array);

  return Array.from(array)
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

async function generateSha384(val) {
  const encoder = new TextEncoder();
  const msgBuffer = encoder.encode(val);
  const hashBuffer = await crypto.subtle.digest('SHA-384', msgBuffer);

  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

async function validateSha384(source, targetHash) {
  if (!source || !targetHash) {
    return false;
  }

  const sourceHash = await generateSha384(source);
  const maxLength = Math.max(sourceHash.length, targetHash.length);
  let differences = 0;

  for (let i = 0; i < maxLength; i++) {
    const charCodeA = sourceHash.charCodeAt(i) || 0;
    const charCodeB = targetHash.charCodeAt(i) || 0;
    differences |= charCodeA ^ charCodeB;
  }

  return differences === 0;
}

/*! js-cookie v3.0.4 | MIT */
;
(function (global, factory) {
  typeof exports === 'object' && typeof module !== 'undefined' ? module.exports = factory() :
  typeof define === 'function' && define.amd ? define(factory) :
  (global = typeof globalThis !== 'undefined' ? globalThis : global || self, (function () {
    var current = global.Cookies;
    var exports = global.Cookies = factory();
    exports.noConflict = function () { global.Cookies = current; return exports; };
  })());
})(this, (function () { 'use strict';

  function assign (target) {
    for (var i = 1; i < arguments.length; i++) {
      var source = arguments[i];
      for (var key in source) {
        target[key] = source[key];
      }
    }
    return target
  }

  var defaultConverter = {
    read: function (value) {
      if (value[0] === '"') {
        value = value.slice(1, -1);
      }
      return value.replace(/(%[\dA-F]{2})+/gi, decodeURIComponent)
    },
    write: function (value) {
      return encodeURIComponent(value).replace(
        /%(2[346BF]|3[AC-F]|40|5[BDE]|60|7[BCD])/g,
        decodeURIComponent
      )
    }
  };

  function init (converter, defaultAttributes) {
    function set (name, value, attributes) {
      if (typeof document === 'undefined') {
        return
      }

      attributes = assign({}, defaultAttributes, attributes);

      if (typeof attributes.expires === 'number') {
        attributes.expires = new Date(Date.now() + attributes.expires * 864e5);
      }
      if (attributes.expires) {
        attributes.expires = attributes.expires.toUTCString();
      }

      name = encodeURIComponent(name)
        .replace(/%(2[346B]|5E|60|7C)/g, decodeURIComponent)
        .replace(/[()]/g, escape);

      var stringifiedAttributes = '';
      for (var attributeName in attributes) {
        if (!attributes[attributeName]) {
          continue
        }

        stringifiedAttributes += '; ' + attributeName;

        if (attributes[attributeName] === true) {
          continue
        }

        stringifiedAttributes += '=' + attributes[attributeName].split(';')[0];
      }

      return (document.cookie =
        name + '=' + converter.write(value, name) + stringifiedAttributes)
    }

    function get (name) {
      if (typeof document === 'undefined' || (arguments.length && !name)) {
        return
      }

      var cookies = document.cookie ? document.cookie.split('; ') : [];
      var jar = {};
      for (var i = 0; i < cookies.length; i++) {
        var parts = cookies[i].split('=');
        var value = parts.slice(1).join('=');

        try {
          var found = decodeURIComponent(parts[0]);
          jar[found] = converter.read(value, found);

          if (name === found) {
            break
          }
        } catch (e) {}
      }

      return name ? jar[name] : jar
    }

    return Object.create(
      {
        set,
        get,
        remove: function (name, attributes) {
          set(
            name,
            '',
            assign({}, attributes, {
              expires: -1
            })
          );
        },
        withAttributes: function (attributes) {
          return init(this.converter, assign({}, this.attributes, attributes))
        },
        withConverter: function (converter) {
          return init(assign({}, this.converter, converter), this.attributes)
        }
      },
      {
        attributes: { value: Object.freeze(defaultAttributes) },
        converter: { value: Object.freeze(converter) }
      }
    )
  }

  var api = init(defaultConverter, { path: '/' });

  return api;

}));

class HashStorageManager {
    static getHashKey(encodedUrl) {
        return `cfsHashStorage:${encodedUrl}`;
    }

    static storeHash(encodedUrl, hash) {
        if (hash) {
          sessionStorage.setItem(this.getHashKey(encodedUrl), hash);
        }
    }

    static retrieveAndClearHash(encodedUrl) {
        const key = this.getHashKey(encodedUrl);
        const hash = sessionStorage.getItem(key) || "";
        sessionStorage.removeItem(key);
        return hash;
    }
}

const FIFTEEN_MINUTES_FROM_NOW = new Date(Date.now() + 15 * 60 * 1000);
const CF_COOKIE_POLICY = 'CloudFront-Policy';
const CF_COOKIE_KEY_PAIR_ID = 'CloudFront-Key-Pair-Id';
const CF_COOKIE_EXPIRATION = 'CloudFront-Expiration';
const CF_COOKIE_SIGNATURE = 'CloudFront-Signature';
const CF_COOKIE_RFP = 'CloudFront-RFP';
const COOKIE_CONFIG = {
  secure: true,
  sameSite: 'Lax',
  expires: FIFTEEN_MINUTES_FROM_NOW
};

const referrer = window.location.href.split("#")[0];
const hash = window.location.hash;
const currentOrigin = window.location.origin;

async function isValidRequest(policy, kpid, exp, sig, rfp, nonceHash) {
  if (!(policy && kpid && exp && sig && rfp && nonceHash)) {
    return false;
  }
  const computedHash = await generateSha384(rfp);
  return validateSha384(`${computedHash}:${sig}`, nonceHash);
}

function clearStaleParentDomainCookies() {
  const hostParts = window.location.hostname.split('.');
  for (let i = 1; i < hostParts.length - 1; i++) {
    const parentDomain = '.' + hostParts.slice(i).join('.');
    const parentConfig = { domain: parentDomain, path: '/' };
    Cookies.remove(CF_COOKIE_POLICY, parentConfig);
    Cookies.remove(CF_COOKIE_KEY_PAIR_ID, parentConfig);
    Cookies.remove(CF_COOKIE_EXPIRATION, parentConfig);
    Cookies.remove(CF_COOKIE_SIGNATURE, parentConfig);
    Cookies.remove(CF_COOKIE_RFP, parentConfig);
  }
}

async function main() {
  clearStaleParentDomainCookies();

  const rfp = Cookies.get(CF_COOKIE_RFP);
  const nonceHash = getParameterInHashByName('nonceHash');
  const policy = getParameterInHashByName('policy');
  const kpid = getParameterInHashByName('kpid');
  const exp = getParameterInHashByName('exp');
  const sig = getParameterInHashByName('sig');
  const encodedTargetUrl = getParameterInHashByName('encodedTargetUrl');
  let decodedTargetUrl = encodedTargetUrl;
  if (!false && encodedTargetUrl) {
    decodedTargetUrl = decodeURIComponent(encodedTargetUrl);
  }
  const validTargetURL = getValidURL(decodedTargetUrl)

  let redirectUrl = referrer;
  if (encodedTargetUrl) {
    redirectUrl = validTargetURL && currentOrigin === validTargetURL.origin
      ? decodedTargetUrl
      : currentOrigin;
  }

  if (await isValidRequest(policy, kpid, exp, sig, rfp, nonceHash)) {
    COOKIE_CONFIG.expires = new Date(Number(exp) * 1000);
    Cookies.set(CF_COOKIE_POLICY, policy, COOKIE_CONFIG);
    Cookies.set(CF_COOKIE_KEY_PAIR_ID, kpid, COOKIE_CONFIG);
    Cookies.set(CF_COOKIE_EXPIRATION, exp, COOKIE_CONFIG);
    Cookies.set(CF_COOKIE_SIGNATURE, sig, COOKIE_CONFIG);

    const storedHash = HashStorageManager.retrieveAndClearHash(encodeURIComponent(redirectUrl));
    window.location.replace(redirectUrl + storedHash);
  } else {
    const generatedRfp = generateRandomBytes();
    const generatedNonce = await generateSha384(generatedRfp);
    Cookies.set(CF_COOKIE_RFP, generatedRfp, COOKIE_CONFIG)

    if (Cookies.get(CF_COOKIE_POLICY)) {
      await new Promise(resolve => setTimeout(resolve, 1000));
    }

    if (!encodedTargetUrl) {
      HashStorageManager.storeHash(encodeURIComponent(redirectUrl), hash);
    }
    window.location.replace(`https://cloudfrontsigner.ninjas.security.a2z.com/v2/authorize?encodedTargetUrl=${encodeURIComponent(redirectUrl)}&cfsNonce=${generatedNonce}`);
  }
}

main()
