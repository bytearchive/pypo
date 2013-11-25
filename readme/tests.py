from unittest.mock import patch, Mock
from django.core.management import call_command
import haystack
import os
from django.test import TestCase
from django.test.client import Client
from django.contrib.auth.models import User
from django.test.utils import override_settings
import requests
from .models import Item
from readme.scrapers import parse
from readme import serializers, download
from rest_framework.exceptions import ParseError
from unittest import mock

EXAMPLE_COM = 'http://www.example.com/'


class BasicTests(TestCase):
    fixtures = ['users.json']
    
    def test_item_user_relation(self):
        user = User.objects.get(pk=1)
        item = Item()
        item.url = 'http://www.example.com'
        item.title = 'Title'
        item.owner = user
        item.save()
        self.assertTrue(item.owner)

    def test_summary(self):
        item = Item()
        item.readable_article = 'lorem_ipsum' * 100
        self.assertEqual(item.summary, item.readable_article[:300])

    def test_unknown_tld(self):
        item = Item()
        item.url = 'foobar'
        self.assertEqual(item.domain, None)


class SerializerTest(TestCase):

    tags = "foo bar baz".split()

    def test_taglist_from_native_accepts_list(self):
        serializer = serializers.TagListSerializer()
        self.assertEqual(self.tags, serializer.from_native(self.tags))

    def test_taglist_from_native_fails_for_non_lists(self):
        serializer = serializers.TagListSerializer()
        with self.assertRaises(ParseError):
            serializer.from_native({'not': 'a list'})

    def test_taglist_to_native_accepts_tag_manager(self):
        mock_tag_manager = mock.Mock(all=mock.Mock())
        mock_tags = []
        for tag_name in self.tags:
            tag = mock.Mock()
            tag.name = tag_name
            mock_tags.append(tag)
        mock_tag_manager.all.return_value = mock_tags
        serializer = serializers.TagListSerializer()
        result = serializer.to_native(mock_tag_manager)
        for tag in self.tags:
            self.assertIn(tag, result)
        mock_tag_manager.all.assert_called()

    def test_taglist_to_native_accepts_lists(self):
        serializer = serializers.TagListSerializer()
        tags = "foo bar baz".split()
        self.assertEqual(tags, serializer.to_native(tags))


    def test_taglist_to_native_fails_otherwise(self):
        serializer = serializers.TagListSerializer()
        with self.assertRaises(ParseError):
            serializer.to_native("not a list")

class ScraperText(TestCase):
    fixtures = ['users.json']

    def test_invalid_html(self):
        item = Item.objects.create(url='http://some_invalid_localhost', domain='nothing', owner=User.objects.get(pk=1))
        self.assertEqual((item.url, ''), parse(item, content_type='text/html', text=None))


def login():
    c = Client()
    assert c.login(username='dev', password='dev')
    return c


class UnknownUserTest(TestCase):
    fixtures = ['users.json']

    def test_item_access_restricted_to_owners(self):
        c = login()
        item = Item.objects.create(url='http://some_invalid_localhost', domain='nothing',
                                   owner=User.objects.create(username='somebody', password='something'))
        response = c.get('/view/{}'.format(item.id))
        self.assertEqual(302, response.status_code, 'User did not get redirected trying to access to a foreign item')

    def test_login_required(self):
        item = Item.objects.create(url='http://some_invalid_localhost', domain='nothing',
                                   owner=User.objects.create(username='somebody', password='something'))
        urls = ['', '/add/', '/view/{}'.format(item.id), '/delete/{}'.format(item.id), '/search/']
        c = Client()
        for url in urls:
            response = c.get(url)
            self.assertEqual(302, response.status_code, 'url {} did not redirect for an anonymus user'.format(url))


class ExistingUserIntegrationTest(TestCase):
    fixtures = ['users.json']

    def test_add_item(self):
        c = login()
        response = c.post('/add/', {'url': EXAMPLE_COM}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, EXAMPLE_COM)

    def test_update_item_updates_tags(self):
        c = login()
        item = Item.objects.create(url=EXAMPLE_COM, domain='nothing', owner=User.objects.get(pk=1))
        item.tags.add('foo', 'bar')
        c.post('/add/', {'url': item.url, 'tags': 'bar baz'})
        new_item = Item.objects.get(id=item.id)
        self.assertEqual(set(['bar', 'baz']), set(new_item.tags.names()))

    def test_tags_are_shown_in_the_list(self):
        c = login()
        item = Item.objects.create(url=EXAMPLE_COM, domain='nothing', owner=User.objects.get(pk=1))
        item.tags.add('foo-tag', 'bar-tag')
        response = c.get('/')
        self.assertContains(response, 'foo-tag')
        self.assertContains(response, 'bar-tag')


TEST_INDEX = {
    'default': {
        'ENGINE': 'haystack.backends.whoosh_backend.WhooshEngine',
        'PATH': os.path.join(os.path.dirname(__file__), 'whoosh_index_test'),
        },
    }

@override_settings(HAYSTACK_CONNECTIONS=TEST_INDEX)
class SearchIntegrationTest(TestCase):
    fixtures = ['users.json']

    def setUp(self):
        haystack.connections.reload('default')
        super(TestCase, self).setUp()

    def tearDown(self):
        call_command('clear_index', interactive=False, verbosity=0)

    def test_search_item_by_title(self):
        c = login()
        Item.objects.create(url=EXAMPLE_COM, title='Example test',
                            owner=User.objects.get(username='dev'),
                             readable_article='test')
        response = c.get('/search/', {'q': 'Example test'})
        self.assertContains(response, 'Results')
        self.assertEqual(1, len(response.context['page'].object_list),
                          'Could not find the test item')

    def test_search_item_by_tag(self):
        c = login()
        item = Item.objects.create(url=EXAMPLE_COM, title='Example test',
                            owner=User.objects.get(username='dev'),
                            readable_article='test')
        item.tags.add('example-tag')
        item.save()
        response = c.get('/search/', {'q': 'example-tag'})
        self.assertContains(response, 'Results')
        self.assertEqual(1, len(response.context['page'].object_list),
                         'Could not find the test item')


@patch('requests.get')
class DownloadTest(TestCase):

    def _mock_content(self, get_mock, content, content_type="", content_length=1, encoding=None):
        return_mock = Mock(headers={'content-type': content_type,
                                    'content-length': content_length},
                           encoding=encoding)
        return_mock.iter_content.return_value = iter([content])
        get_mock.return_value = return_mock

    def test_uses_request_to_start_the_download(self, get_mock):
        get_mock.side_effect = requests.RequestException
        with self.assertRaises(download.DownloadException):
            download.download(EXAMPLE_COM)
        get_mock.assert_called_with(EXAMPLE_COM, stream=True)

    def test_aborts_large_downloads(self, get_mock):
        max_length = 1000
        return_mock = Mock(headers={'content-length': max_length+1})
        get_mock.return_value = return_mock
        with self.assertRaises(download.DownloadException) as cm:
            download.download(EXAMPLE_COM, max_length)
        self.assertIn('content-length', cm.exception.message)

    def test_aborts_with_invalid_headers(self, get_mock):
        return_mock = Mock(headers={'content-length': "invalid"})
        get_mock.return_value = return_mock
        with self.assertRaises(download.DownloadException) as cm:
            download.download(EXAMPLE_COM)
        self.assertIn('content-length', cm.exception.message)
        self.assertIn('convert', cm.exception.message)
        self.assertIsInstance(cm.exception.parent, ValueError)

    def test_only_downloads_up_to_a_maximum_length(self, get_mock):
        content = Mock()
        max_length = 1
        self._mock_content(get_mock, content=content, content_length=max_length)
        ret = download.download(EXAMPLE_COM, max_content_length=max_length)
        get_mock.return_value.iter_content.assert_called_with(max_length)
        self.assertEqual(ret.content, content)

    def test_decodes_text_content(self, get_mock):
        content, encoding = Mock(), Mock()
        content.decode.return_value = 'text'
        self._mock_content(get_mock, content=content, content_type='text/html', encoding=encoding)
        ret = download.download(EXAMPLE_COM)
        content.decode.assert_called_with(encoding, errors='ignore')
        self.assertEqual('text', ret.text)

    def test_ignores_invalid_decode(self, get_mock):
        content, encoding = "üöä".encode('utf-8'), 'ascii'
        self._mock_content(get_mock, content=content, content_type='text/html', encoding=encoding)
        ret = download.download(EXAMPLE_COM)
        # expect the empty fallback text because the decode had only errors
        self.assertEqual('', ret.text)

    def test_only_decodes_text_content(self, get_mock):
        content = Mock()
        self._mock_content(get_mock, content=content, content_type="something/else")
        ret = download.download(EXAMPLE_COM)
        # expect the empty fallback text because the decode failed
        self.assertEqual(None, ret.text)








