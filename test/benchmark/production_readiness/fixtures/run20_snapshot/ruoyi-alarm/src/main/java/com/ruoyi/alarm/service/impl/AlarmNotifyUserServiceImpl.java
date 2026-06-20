package com.ruoyi.alarm.service.impl;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.domain.AlarmNotifyUser;
import com.ruoyi.alarm.mapper.AlarmNotifyUserMapper;
import com.ruoyi.alarm.service.IAlarmNotifyUserService;

/**
 * 通知用户 Service业务层处理
 *
 * @author ruoyi
 */
@Service
public class AlarmNotifyUserServiceImpl implements IAlarmNotifyUserService
{
    @Autowired
    private AlarmNotifyUserMapper alarmNotifyUserMapper;

    /**
     * 查询通知用户
     *
     * @param userId 用户ID
     * @return 通知用户
     */
    @Override
    public AlarmNotifyUser selectAlarmNotifyUserById(Long userId)
    {
        return alarmNotifyUserMapper.selectAlarmNotifyUserById(userId);
    }

    /**
     * 查询通知用户列表
     *
     * @param alarmNotifyUser 通知用户
     * @return 通知用户集合
     */
    @Override
    public List<AlarmNotifyUser> selectAlarmNotifyUserList(AlarmNotifyUser alarmNotifyUser)
    {
        return alarmNotifyUserMapper.selectAlarmNotifyUserList(alarmNotifyUser);
    }

    /**
     * 新增通知用户
     *
     * @param alarmNotifyUser 通知用户
     * @return 结果
     */
    @Override
    public int insertAlarmNotifyUser(AlarmNotifyUser alarmNotifyUser)
    {
        return alarmNotifyUserMapper.insertAlarmNotifyUser(alarmNotifyUser);
    }

    /**
     * 修改通知用户
     *
     * @param alarmNotifyUser 通知用户
     * @return 结果
     */
    @Override
    public int updateAlarmNotifyUser(AlarmNotifyUser alarmNotifyUser)
    {
        return alarmNotifyUserMapper.updateAlarmNotifyUser(alarmNotifyUser);
    }

    /**
     * 删除通知用户
     *
     * @param userIds 需要删除的用户ID
     * @return 结果
     */
    @Override
    public int deleteAlarmNotifyUserByIds(Long[] userIds)
    {
        return alarmNotifyUserMapper.deleteAlarmNotifyUserByIds(userIds);
    }

    /**
     * 根据用户ID列表查询通知用户
     *
     * @param userIds 用户ID列表
     * @return 通知用户集合
     */
    @Override
    public List<AlarmNotifyUser> selectAlarmNotifyUserByIds(List<Long> userIds)
    {
        return alarmNotifyUserMapper.selectAlarmNotifyUserByIds(userIds);
    }
}
