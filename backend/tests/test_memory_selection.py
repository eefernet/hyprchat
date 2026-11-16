"""The memory block of a chat's prompt stays the same from turn to turn.

Measured 2026-11-15: the block was re-picked by word overlap with every new message and listed in score
order, so its text changed each turn and Ollama read the whole chat again (1.1 to 1.4 s per turn on a
9,000-token chat with memory on, 0.11 s with it off).
"""
import asyncio

import pytest

import database as db
import memory_selection

TOPICS = ['dog Biscuit beagle', 'hiking trails Rockies', 'coffee espresso oat', 'Python typing hints', 'car Subaru Outback',
          'birthday March cake', 'guitar Telecaster blues', 'garden tomatoes basil', 'sister Maya Denver', 'chess Sicilian openings',
          'bread sourdough starter', 'keyboard ortholinear switches', 'running marathon Boston', 'camera Fujifilm lenses',
          'tea oolong evening', 'novel Dune rereading', 'bike gravel tires', 'movie Arrival favourite', 'server homelab Proxmox',
          'cat Miso tabby', 'language Spanish lessons', 'pizza Neapolitan oven', 'piano Chopin nocturnes', 'ski Whistler powder',
          'aquarium tetras plants', 'woodworking walnut table', 'podcast history Rome']


def memory(n, content, **extra):
    return {'id': f'mem-{n:03d}', 'content': content, 'importance': 3, 'pinned': 0, 'created_at': f'2026-01-{n + 1:02d}', **extra}


def shares(*words):
    return lambda item: sum(word in item['content'] for word in words)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'memory.db'))

    async def seed(contents, **extra):
        await db.init_db()
        for content in contents:
            await db.create_global_memory(content=content, status='accepted', **extra)
    return seed


def test_the_memory_block_is_the_same_for_two_different_questions_when_everything_fits(store):
    async def run():
        await store([f'The user cares about {topic}.' for topic in TOPICS])      # 27, like the live profile
        first = await db.build_global_memory_context(query='a tip about my dog and hiking trails')
        second = await db.build_global_memory_context(query='something on coffee and Python typing')
        third = await db.build_global_memory_context(query='')
        return first, second, third
    first, second, third = asyncio.run(run())
    assert first['context'] == second['context'] == third['context']
    assert first['memory_ids'] == second['memory_ids'] and len(first['memory_ids']) == 27      # all of them, not twelve
    for topic in TOPICS:
        assert topic in first['context']


def test_a_workspace_memory_block_does_not_change_with_the_question(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'workspace.db'))

    async def run():
        await db.init_db()
        workspace = await db.create_workspace('ws-1', 'Notes')
        await db.update_workspace('ws-1', memory_enabled=1)
        for topic in TOPICS[:14]:
            await db.create_workspace_memory('ws-1', content=f'Decision about {topic}.', status='accepted')
        first = await db.build_workspace_memory_context(workspace_id='ws-1', query='dog hiking')
        second = await db.build_workspace_memory_context(workspace_id='ws-1', query='coffee Python')
        return first, second
    first, second = asyncio.run(run())
    assert first['context'] == second['context'] and len(first['memory_ids']) == 14


def test_when_they_do_not_all_fit_the_most_relevant_are_chosen_and_listed_in_a_fixed_order():
    memories = [memory(n, f'Long note about {topic}. ' + 'detail ' * 30) for n, topic in enumerate(TOPICS)]
    about_dogs = memory_selection.choose(memories, room=1500, cap=12, overlap=shares('dog', 'hiking'))
    about_tea = memory_selection.choose(memories, room=1500, cap=12, overlap=shares('tea', 'chess'))
    assert 0 < len(about_dogs) < len(memories)
    assert any('dog' in line for _, line in about_dogs) and any('hiking' in line for _, line in about_dogs)
    assert any('tea oolong' in line for _, line in about_tea) and any('chess' in line for _, line in about_tea)
    assert not any('tea oolong' in line or 'chess' in line for _, line in about_dogs)     # room left over goes to the first stored
    for chosen in (about_dogs, about_tea):                       # whatever was chosen is listed oldest first
        assert [item['id'] for item, _ in chosen] == sorted(item['id'] for item, _ in chosen)
        assert sum(len(line) for _, line in chosen) <= 1500


def test_no_more_than_the_cap_are_sent_when_they_do_not_fit():
    memories = [memory(n, f'Note {n} ' + 'x' * 60) for n in range(40)]
    assert len(memory_selection.choose(memories, room=1000, cap=5, overlap=lambda item: 0)) == 5
    assert len(memory_selection.choose(memories, room=100000, cap=5, overlap=lambda item: 0)) == 40   # all fit: all are sent


def test_pinned_and_important_memories_are_listed_first_whatever_the_question():
    memories = [memory(0, 'ordinary'), memory(1, 'important', importance=5), memory(2, 'pinned', pinned=1), memory(3, 'ordinary too')]
    for overlap in (lambda item: 0, shares('ordinary')):
        lines = [line for _, line in memory_selection.choose(memories, room=1000, cap=12, overlap=overlap)]
        assert lines == ['- [pinned] pinned', '- important', '- ordinary', '- ordinary too']


def test_empty_memories_are_left_out_and_a_long_one_is_cut_to_the_room_left():
    memories = [memory(0, '   '), memory(1, 'short'), memory(2, 'y' * 400)]
    chosen = memory_selection.choose(memories, room=100, cap=12, overlap=lambda item: 0)
    assert [item['id'] for item, _ in chosen] == ['mem-001', 'mem-002']
    assert all(len(line) <= 100 for _, line in chosen) and chosen[1][1].endswith('...')
    assert memory_selection.choose([], room=100, cap=12, overlap=lambda item: 0) == []
