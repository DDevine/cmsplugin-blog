import datetime

from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import signals
from django.db.models.query import QuerySet
from django.conf import settings
from django.utils.translation import get_language, ugettext_lazy as _

from cms.utils.placeholder import PlaceholderNoAction
from cms.utils.urlutils import urljoin

from cms.models import CMSPlugin, Title

import tagging
from tagging.fields import TagField

from simple_translation.actions import SimpleTranslationPlaceholderActions
from djangocms_utils.fields import M2MPlaceholderField

from threadedcomments import ThreadedComment
from django.contrib.comments.signals import comment_was_posted
from django.contrib.sites.models import Site

class PublishedEntriesQueryset(QuerySet):
    
    def published(self):
        return self.filter(is_published=True, pub_date__lte=datetime.datetime.now())
        
class EntriesManager(models.Manager):
    
    def get_query_set(self):
        return PublishedEntriesQueryset(self.model)
                            
class PublishedEntriesManager(EntriesManager):
    """
        Filters out all unpublished and items with a publication date in the future
    """
    def get_query_set(self):
        return super(PublishedEntriesManager, self).get_query_set().published()
                    
CMSPLUGIN_BLOG_PLACEHOLDERS = getattr(settings, 'CMSPLUGIN_BLOG_PLACEHOLDERS', ('excerpt', 'content'))
              
class Entry(models.Model):
    is_published = models.BooleanField(_('is published'))
    pub_date = models.DateTimeField(_('publish at'), default=datetime.datetime.now)
 
    placeholders = M2MPlaceholderField(actions=SimpleTranslationPlaceholderActions(), placeholders=CMSPLUGIN_BLOG_PLACEHOLDERS)
    
    tags = TagField()
    
    objects = EntriesManager()
    published = PublishedEntriesManager()

    def get_absolute_url(self, language=None):
        if not language:
            language = get_language()
        try:
            url = self.entrytitle_set.get(language=language).get_absolute_url()
            if url[1:len(language)+1] == language:
                url = url[len(language)+1:]
            return url
        except EntryTitle.DoesNotExist:
            return ''

    def language_changer(self, language):
        url = self.get_absolute_url(language)
        if url:
            return url

        # There is no entry in the given language, we return blog's root

        blog_prefix = ''

        try:
            title = Title.objects.get(application_urls='BlogApphook', language=language)
            blog_prefix = urljoin(reverse('pages-root'), title.overwrite_url or title.slug)
        except Title.DoesNotExist:
            # Blog app hook not defined anywhere?
            pass

        return blog_prefix or reverse('pages-root')
     
    class Meta:
        verbose_name = _('entry')
        verbose_name_plural = _('entries')
        ordering = ('-pub_date', )

tagging.register(Entry, tag_descriptor_attr='entry_tags')

def close_comments_date():
    return datetime.date.today()

def moderate_comments_date():
    return datetime.date.today() - datetime.timedelta(days=getattr(settings, 'CMSPLUGIN_BLOG_MOD_CLOSE_AFTER', 7))

class AbstractEntryTitle(models.Model):
    entry = models.ForeignKey(Entry, verbose_name=_('entry'))
    language = models.CharField(_('language'), max_length=15, choices=settings.LANGUAGES)
    title = models.CharField(_('title'), max_length=255)
    slug = models.SlugField(_('slug'), max_length=255)
    author = models.ForeignKey('auth.User', null=True, blank=True, verbose_name=_("author"))
    
    comments_active = models.BooleanField(_('Activate comments'), help_text=_('If this is disabled comment forms and existing comments won\'t be displayed.'), default=True)
    comments_enabled = models.BooleanField(_('enable comments'), help_text=_('Freeze or unfreeze commenting. Existing comments will be displayed.'), default=True )

    def __unicode__(self):
        return self.title
        
    def _get_absolute_url(self):
        language_namespace = 'cmsplugin_blog.middleware.MultilingualBlogEntriesMiddleware' in settings.MIDDLEWARE_CLASSES and '%s:' % self.language or ''
        return ('%sblog_detail' % language_namespace, (), {
            'year': self.entry.pub_date.strftime('%Y'),
            'month': self.entry.pub_date.strftime('%m'),
            'day': self.entry.pub_date.strftime('%d'),
            'slug': self.slug
        })
    get_absolute_url = models.permalink(_get_absolute_url)

    class Meta:
        unique_together = ('language', 'slug')
        verbose_name = _('blogentry')
        verbose_name_plural = _('blogentries')
        abstract = True
        
    def _pub_date(self):
        return self.entry.pub_date
    pub_date = property(_pub_date)
    
    def close_days(self):
        pub_date = datetime.date(self.pub_date.year, self.pub_date.month, self.pub_date.day)
        return (pub_date - datetime.date.today()).days
        
    def comments_closed(self):
        if not self.comments_enabled:
            return True
        if self.pub_date < datetime.date.today() or self.close_days() >= getattr(settings, 'CMSPLUGIN_BLOG_MOD_CLOSE_AFTER', 7):
            return True
        return False

    def comments_disabled(self):
	if not self.comments_active:
	   return True
	else:
	   return False
    
    def moderate_days(self): # not needed for now remove or leave as hook?
        pub_date = datetime.date(self.pub_date.year, self.pub_date.month, self.pub_date.day)
        return (pub_date - datetime.date.today()).days
            
    def comments_under_moderation(self):
        return getattr(settings,'CMSPLUGIN_BLOG_MODERATE', True)

class EntryTitle(AbstractEntryTitle):
    pass
       
class LatestEntriesPlugin(CMSPlugin):
    """
        Model for the settings when using the latest entries cms plugin
    """
    limit = models.PositiveIntegerField(_('Number of entries items to show'), 
                    help_text=_('Limits the number of items that will be displayed'))
                    
    current_language_only = models.BooleanField(_('Only show entries for the current language'))

from django.contrib.comments.moderation import CommentModerator, moderator

class EntryModerator(CommentModerator):
    enable_field = 'comments_enabled'
    auto_close_field = 'pub_date'
    close_after = getattr(settings, 'CMSPLUGIN_BLOG_MOD_CLOSE_AFTER', 7)
        
    def moderate(self, comment, content_object, request):
        return True

if getattr(settings, 'CMSPLUGIN_BLOG_MODERATE', False):
    moderator.register(EntryTitle, EntryModerator)

def on_comment_was_posted_notification(sender, comment, request, *args, **kwargs):
    # Sends a templated email to the origional poster on reply.
    from django.core.mail import send_mail
    from django.template import loader, Context

    to_list = [comment.parent.email] 

    context_dict = {'user_name': comment.user_name.encode('utf-8'),
		    'user_url': comment.user_url,
                    'submit_date': comment.submit_date,
		    'comment': comment.comment,
                    'comment_url': comment.content_object.get_absolute_url,
		    }

    print context_dict

    nodes = dict((n.name, n) for n in loader.get_template('email_replied').nodelist if n.__class__.__name__ == 'BlockNode')
    con = Context(context_dict)
    r = lambda n: nodes[n].render(con)

    for address in to_list:
        send_mail(r('subject'), r(getattr(settings, 'CMSPLUGIN_BLOG_EMAIL_TYPE', 'plain'), getattr(settings, 'CMSPLUGIN_BLOG_EMAIL_FROM', "blog@%s" % (Site.objects.get_current().domain)), [address,])


if getattr(settings, 'CMSPLUGIN_BLOG_COMMENT_NOTIFICATIONS', False):
    comment_was_posted.connect(on_comment_was_posted_notification)

def on_comment_was_posted_spamcheck(sender, comment, request, *args, **kwargs):
    """ 
        Defines action for django.contrib.comments on_comment_was_posted signal. 
        Provides anti-spam via the Akismet and TypePad Antispam services. Both services require the akismet package.
        Fails silently if the Akismet package is not available. 
    """
    try:
        from akismet import Akismet
    except:
        return

    # use TypePad's AntiSpam if the key is specified in settings.py
    if hasattr(settings, 'TYPEPAD_ANTISPAM_API_KEY'):
        ak = Akismet(
            key=settings.TYPEPAD_ANTISPAM_API_KEY,
            blog_url='http://%s/' % Site.objects.get(pk=settings.SITE_ID).domain
        )
        ak.baseurl = 'api.antispam.typepad.com/1.1/'
    else:
        ak = Akismet(
            key=settings.AKISMET_API_KEY,
            blog_url='http://%s/' % Site.objects.get(pk=settings.SITE_ID).domain
        )

    if ak.verify_key():
        data = {
            'user_ip': request.META.get('REMOTE_ADDR', '127.0.0.1'),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            'referrer': request.META.get('HTTP_REFERER', ''),
            'comment_type': 'comment',
            'comment_author': comment.user_name.encode('utf-8'),
        }

        if ak.comment_check(comment.comment.encode('utf-8'), data=data, build_data=True):
            comment.flags.create(
                user=comment.content_object.author,
                flag='spam'
            )
            comment.is_public = False
            comment.save()

if getattr(settings, 'CMSPLUGIN_BLOG_SPAM_FILTER', False):
    comment_was_posted.connect(on_comment_was_posted_spamcheck)

