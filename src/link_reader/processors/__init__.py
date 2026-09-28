from .audio import AudioProcessor
from .epub import EPUBProcessor
from .pdf import PDFProcessor
from .podcast import PodcastProcessor
from .reddit import RedditProcessor
from .social import SocialVideoProcessor
from .webpage import WebPageProcessor
from .youtube import YouTubeProcessor


def build_processors(settings):
    """Single processor registry shared by every transport/channel."""
    return [
        YouTubeProcessor(settings),
        PDFProcessor(settings),
        EPUBProcessor(settings),
        AudioProcessor(settings),
        PodcastProcessor(settings),
        RedditProcessor(settings),
        SocialVideoProcessor(settings),
        WebPageProcessor(settings),
    ]


__all__ = [
    "YouTubeProcessor",
    "PDFProcessor",
    "EPUBProcessor",
    "PodcastProcessor",
    "RedditProcessor",
    "AudioProcessor",
    "SocialVideoProcessor",
    "WebPageProcessor",
    "build_processors",
]
