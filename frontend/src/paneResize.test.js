import test from 'node:test';
import assert from 'node:assert/strict';
import {maxWidth,clampWidth,draggedWidth,keyWidth,storedWidth,MIN_WIDTH,WIDE_ENOUGH,CHAT_ROOM,KEY_STEP,DEFAULT_WIDTH} from './paneResize.js';

test('900 is always allowed; a wide window allows more and leaves the chat its room',()=>{
  assert.equal(maxWidth(1000),WIDE_ENOUGH);
  assert.equal(maxWidth(0),WIDE_ENOUGH);assert.equal(maxWidth(undefined),WIDE_ENOUGH);assert.equal(maxWidth(NaN),WIDE_ENOUGH);
  assert.equal(maxWidth(1800),1800-CHAT_ROOM);
  assert.equal(maxWidth(1320.6),900);
});

test('widths are whole numbers inside the limits',()=>{
  assert.equal(clampWidth(100,1800),MIN_WIDTH);
  assert.equal(clampWidth(5000,1800),1380);
  assert.equal(clampWidth(5000,1000),900);
  assert.equal(clampWidth(512.6,1800),513);
  assert.equal(clampWidth('x',1800),DEFAULT_WIDTH);
});

test('dragging left widens, right narrows, by the distance moved',()=>{
  assert.equal(draggedWidth(500,1000,900,1,2000),600);
  assert.equal(draggedWidth(500,1000,1100,1,2000),400);
  assert.equal(draggedWidth(500,1000,1000,1,2000),500);
  assert.equal(draggedWidth(500,1000,1900,1,2000),MIN_WIDTH);
  assert.equal(draggedWidth(500,1000,-4000,1,2000),1580);
});

test('the distance is divided by the UI scale so the edge stays under the cursor',()=>{
  // UI size 16 of 14: the app is drawn 8/7 larger, so 80 screen pixels are 70 of the pane's.
  assert.equal(draggedWidth(500,1000,920,16/14,2000),570);
  // UI size 13 of 14: 65 screen pixels are 70 of the pane's.
  assert.equal(draggedWidth(500,1000,935,13/14,2000),570);
  // A scale that could not be read counts as 1.
  assert.equal(draggedWidth(500,1000,900,0,2000),600);assert.equal(draggedWidth(500,1000,900,undefined,2000),600);
});

test('arrow keys move the edge the way they point; Home and End go to the limits',()=>{
  assert.equal(keyWidth(500,'ArrowLeft',2000),500+KEY_STEP);
  assert.equal(keyWidth(500,'ArrowRight',2000),500-KEY_STEP);
  assert.equal(keyWidth(270,'ArrowRight',2000),MIN_WIDTH);
  assert.equal(keyWidth(500,'Home',2000),1580);
  assert.equal(keyWidth(500,'End',2000),MIN_WIDTH);
  assert.equal(keyWidth(500,'Enter',2000),null);
});

test('a stored width that is missing or nonsense falls back to the default',()=>{
  assert.equal(storedWidth('640'),640);
  assert.equal(storedWidth(null),DEFAULT_WIDTH);assert.equal(storedWidth('abc'),DEFAULT_WIDTH);assert.equal(storedWidth('12'),DEFAULT_WIDTH);
});
