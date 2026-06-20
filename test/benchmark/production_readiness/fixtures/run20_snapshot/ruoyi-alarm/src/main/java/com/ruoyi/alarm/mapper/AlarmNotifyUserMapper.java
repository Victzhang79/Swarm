package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmNotifyUser;

/**
 * 通知用户 Mapper
 *
 * @author ruoyi
 */
public interface AlarmNotifyUserMapper
{
    /**
     * 查询通知用户
     *
     * @param userId 用户ID
     * @return 通知用户
     */
    public AlarmNotifyUser selectAlarmNotifyUserById(Long userId);

    /**
     * 查询通知用户列表
     *
     * @param alarmNotifyUser 通知用户
     * @return 通知用户集合
     */
    public List<AlarmNotifyUser> selectAlarmNotifyUserList(AlarmNotifyUser alarmNotifyUser);

    /**
     * 新增通知用户
     *
     * @param alarmNotifyUser 通知用户
     * @return 结果
     */
    public int insertAlarmNotifyUser(AlarmNotifyUser alarmNotifyUser);

    /**
     * 修改通知用户
     *
     * @param alarmNotifyUser 通知用户
     * @return 结果
     */
    public int updateAlarmNotifyUser(AlarmNotifyUser alarmNotifyUser);

    /**
     * 删除通知用户
     *
     * @param userIds 需要删除的用户ID
     * @return 结果
     */
    public int deleteAlarmNotifyUserByIds(Long[] userIds);

    /**
     * 根据用户ID列表查询通知用户
     *
     * @param userIds 用户ID列表
     * @return 通知用户集合
     */
    public List<AlarmNotifyUser> selectAlarmNotifyUserByIds(java.util.List<Long> userIds);
}
