import test from 'node:test';
import assert from 'node:assert/strict';
import {isHtmlFence,isFullHtmlDocument,withPreviewCsp,previewTitle,CSP_POLICIES} from './htmlPreview.js';

test('only html fences are previewable',()=>{
  assert(isHtmlFence('html'));assert(isHtmlFence(' HTML '));assert(isHtmlFence('htm'));
  assert(!isHtmlFence('xml'));assert(!isHtmlFence('svg'));assert(!isHtmlFence(''));assert(!isHtmlFence(undefined));
});
test('full documents are detected through BOM, whitespace and case',()=>{
  assert(isFullHtmlDocument('<!DOCTYPE html><html></html>'));
  assert(isFullHtmlDocument('﻿\n  <!doctype HTML>\n<html>'));
  assert(isFullHtmlDocument('<html lang="en"><body></body></html>'));
  assert(!isFullHtmlDocument('<div>hi</div>'));
  assert(!isFullHtmlDocument('<htmlx>'));
  assert(!isFullHtmlDocument(''));
});
test('the CSP meta is the first child of head, creating head when missing',()=>{
  const withHead=withPreviewCsp('<html><head><title>x</title></head><body></body></html>');
  assert.match(withHead,/^<html><head><meta http-equiv="Content-Security-Policy" content="[^"]+"><title>x<\/title>/);
  const noHead=withPreviewCsp('<html lang="en"><body>b</body></html>');
  assert.match(noHead,/^<html lang="en"><head><meta http-equiv="Content-Security-Policy"[^>]+><\/head><body>b/);
  const fragment=withPreviewCsp('<p>frag</p>');
  assert.match(fragment,/^<!doctype html><html><head><meta http-equiv="Content-Security-Policy"[^>]+><\/head><body><p>frag<\/p><\/body><\/html>$/);
});
test('cdn allows https libraries and strict does not; neither allows network or frames',()=>{
  assert.match(CSP_POLICIES.cdn,/script-src 'unsafe-inline' https:/);
  assert.doesNotMatch(CSP_POLICIES.strict,/https:/);
  for(const policy of Object.values(CSP_POLICIES)){
    assert.match(policy,/connect-src 'none'/);assert.match(policy,/form-action 'none'/);
    assert.match(policy,/frame-src 'none'/);assert.match(policy,/base-uri 'none'/);
  }
  assert.match(withPreviewCsp('<html><head></head></html>','strict'),/script-src 'unsafe-inline';/);
  assert.match(withPreviewCsp('<html><head></head></html>','nonsense'),/https:/);
});
test('preview titles prefer the filename, then <title>, then a default',()=>{
  assert.equal(previewTitle('public/index.html','<title>Ignored</title>'),'index.html');
  assert.equal(previewTitle('','<html><head><title> Pocket tasks </title></head></html>'),'Pocket tasks');
  assert.equal(previewTitle('','<div/>'),'Preview');
});
