from django.db import models

# -------------------------
# DROP-OFF SITE TABLE
# -------------------------
class DropOffSite(models.Model):
    id = models.AutoField(primary_key=True)
    barangay = models.CharField(max_length=100, unique=True)
    assigned_staff = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dropoff_site'
    )

    class Meta:
        managed = True
        db_table = 'dropoff_sites'

    def __str__(self):
        return f"{self.barangay} Drop-Off Site"

# -------------------------
# USER TABLE (Residents & Staff)
# -------------------------
class User(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20, unique=True)
    password_hash = models.TextField()
    user_type = models.CharField(max_length=10)  # 'resident' or 'staff'
    barangay = models.CharField(max_length=100)
    is_approved = models.BooleanField(default=False)
    date_joined = models.DateTimeField()
    account_status = models.CharField(max_length=10)  # 'active' or 'disabled'
    registered_by_admin = models.BooleanField()

    class Meta:
        managed = True
        db_table = 'users'

    def __str__(self):
        return self.name

# -------------------------
# SUBMISSIONS TABLE
# -------------------------
from django.db import models
from .models import User, DropOffSite  # Adjust import if needed

# -------------------------
# SUBMISSIONS TABLE
# -------------------------
class Submission(models.Model):
    id = models.AutoField(primary_key=True)

    # Used to prevent duplicate QR scanning
    qr_id = models.CharField(max_length=100, unique=True, null=True, blank=True)

    user = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column='user_id',
        related_name='submissions',
        null=True,
        blank=True
    )
    staff = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        db_column='staff_id',
        related_name='processed_submissions'
    )
    dropoff_site = models.ForeignKey(
        DropOffSite,
        on_delete=models.DO_NOTHING,
        db_column='dropoff_site_id',
        related_name='submissions'
    )

    # Bottle info stored as: [{'size': '500ml', 'quantity': 3}, ...]
    bottle_data = models.JSONField(default=list)

    total_points = models.IntegerField()
    source = models.CharField(max_length=20, default='manual')

    # Optional image-based submission data
    image_path = models.TextField(blank=True, null=True)
    estimated_quantity = models.IntegerField(blank=True, null=True)
    confidence_score = models.FloatField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'submissions'

    def __str__(self):
        return f"Submission by {self.staff.name} - {self.total_points} pts"


# -------------------------
# STAFF TRANSACTION HISTORY TABLE
# -------------------------
class StaffTransaction(models.Model):
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='staff_id',
        related_name='staff_transactions'
    )
    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name='transaction_record'
    )
    action = models.CharField(max_length=50, default='submission_made')  
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    notes = models.TextField(blank=True, null=True, help_text="Details about this transaction")

    class Meta:
        managed = True
        db_table = 'staff_transactions'

    def __str__(self):
        return f"{self.staff.name} - {self.action} on {self.created_at.strftime('%Y-%m-%d %H:%M')}"


# -------------------------
# REWARD REQUESTS TABLE
# -------------------------
class RewardRequest(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id')
    mobile_number = models.CharField(max_length=20)
    telco = models.CharField(max_length=10)
    points_used = models.IntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10)
    processed_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='processed_by', related_name='processed_rewards', null=True)
    date_requested = models.DateTimeField()
    date_processed = models.DateTimeField(null=True)

    class Meta:
        managed = True
        db_table = 'reward_requests'

    def __str__(self):
        return f"{self.user.name} - ₱{self.amount} ({self.status})"

# -------------------------
# QUIZ CATEGORY TABLE
# -------------------------
class QuizCategory(models.Model):
    id = models.AutoField(primary_key=True)
    category_name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    feedback = models.TextField(blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'quiz_categories'

    def __str__(self):
        return self.category_name

# -------------------------
# QUIZ QUESTIONS TABLE
# -------------------------
class Quiz(models.Model):
    id = models.AutoField(primary_key=True)
    category = models.ForeignKey(
        QuizCategory,
        on_delete=models.DO_NOTHING,
        db_column='category_id',
        related_name='quiz_set',
        default=1
    )
    question = models.TextField()
    option_a = models.TextField()
    option_b = models.TextField()
    option_c = models.TextField()
    option_d = models.TextField()
    correct_option = models.CharField(max_length=1)
    points = models.IntegerField()
    feedback = models.TextField(blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'quizzes'

    def __str__(self):
        return self.question

# -------------------------
# QUIZ ANSWERS TABLE
# -------------------------
class QuizAnswer(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id', default=1)
    quiz = models.ForeignKey(Quiz, on_delete=models.DO_NOTHING, db_column='quiz_id', default=1)
    selected_option = models.CharField(max_length=1)
    is_correct = models.BooleanField()
    date_answered = models.DateTimeField()

    class Meta:
        managed = True
        db_table = 'quiz_answers'

# -------------------------
# QUIZ SESSION TABLE
# -------------------------
class QuizSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id')
    category = models.ForeignKey(QuizCategory, on_delete=models.DO_NOTHING, db_column='category_id')
    score = models.IntegerField()
    max_score = models.IntegerField()
    bonus_awarded = models.BooleanField(default=False)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'quiz_sessions'

# -------------------------
# USER BADGES TABLE
# -------------------------
class UserBadge(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='user_id', default=1)
    badge_name = models.CharField(max_length=100)
    awarded_at = models.DateTimeField()

    class Meta:
        managed = True
        db_table = 'user_badges'
